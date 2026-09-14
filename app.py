#!/usr/bin/env python3
"""Factorio Panel — a modern, single-file management UI for dockerized Factorio servers.
Docker control, RCON, live chat + Discord bridge, saves, mod portal search/install/update
with dependency resolution, validated settings forms, world/map-gen editors, player admin,
users & roles, metrics, off-box backups, raw console, audit log.
https://github.com/Midnigh7/factorio-panel (GPL-3.0)"""
import os, re, json, time, socket, struct, subprocess, threading, secrets, hashlib, hmac, logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from functools import wraps
from flask import Flask, request, session, jsonify, redirect, url_for, send_file, render_template_string, flash
import urllib.request, urllib.parse

APP_DIR = os.environ.get("PANEL_DIR", os.path.expanduser("~/factorio-panel"))
ENV_FILE = os.path.join(APP_DIR, "panel.env")
CREDS_FILE = os.path.join(APP_DIR, "factorio_creds.json")
COMPOSE_FILE = os.environ.get("FACTORIO_COMPOSE", os.path.expanduser("~/factorio/compose.yml"))
HOST_ROOT = os.environ.get("FACTORIO_DATA", "/opt/factorio")  # host view (reads)
CONT_ROOT = os.environ.get("FACTORIO_CONT_ROOT", "/factorio")  # container view (writes via docker exec)
CONTAINER = os.environ.get("FACTORIO_CONTAINER", "factorio")
SAVES_DIR = f"{HOST_ROOT}/saves"
MODS_DIR = f"{HOST_ROOT}/mods"
CONFIG_DIR = f"{HOST_ROOT}/config"
RCON_PW_FILE = f"{CONFIG_DIR}/rconpw"
RCON_HOST = os.environ.get("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.environ.get("RCON_PORT", "27015"))
AUDIT_FILE = os.path.join(APP_DIR, "audit.log")

def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    return env

ENV = load_env()
app = Flask(__name__)
app.secret_key = ENV.get("SECRET_KEY", secrets.token_hex(32))
app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True, MAX_CONTENT_LENGTH=512*1024*1024)

_audit = logging.getLogger("audit")
_h = RotatingFileHandler(AUDIT_FILE, maxBytes=512*1024, backupCount=5)
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
_audit.addHandler(_h); _audit.setLevel(logging.INFO)
def audit(msg): _audit.info(f"[{request.remote_addr if request else '-'}] {msg}")

control_lock = threading.Lock()
control_state = {"countdown": False, "seconds_left": 0, "action": None}

# ── auth: users, roles, sessions ─────────────────────────────────────────────
USERS_FILE = os.path.join(APP_DIR, "users.json")
PANEL_CONFIG_FILE = os.path.join(APP_DIR, "panel_config.json")
ROLES = ["viewer", "moderator", "admin"]

def load_users():
    try: return json.load(open(USERS_FILE))["users"]
    except Exception: return []

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump({"users": users}, f, indent=2)
    os.chmod(USERS_FILE, 0o600)

def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200000).hex()
    return f"pbkdf2${salt}${h}"

def verify_pw(pw, stored):
    try:
        _, salt, h = stored.split("$")
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200000).hex(), h)
    except Exception:
        return False

def find_user(username):
    return next((u for u in load_users() if u["username"].lower() == username.lower()), None)

def check_legacy_password(pw):
    h = ENV.get("PASSWORD_SHA256", "")
    return h and hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), h)

def role_rank(role):
    return ROLES.index(role) if role in ROLES else -1

def current_role():
    return session.get("role", "")

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("auth"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

def role_required(min_role):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if not session.get("auth"):
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "unauthorized"}), 401
                return redirect(url_for("login"))
            if role_rank(current_role()) < role_rank(min_role):
                return jsonify({"ok": False, "error": f"requires {min_role}"}), 403
            return f(*a, **k)
        return w
    return deco

def load_panel_config():
    try: return json.load(open(PANEL_CONFIG_FILE))
    except Exception: return {}

def save_panel_config(cfg):
    with open(PANEL_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(PANEL_CONFIG_FILE, 0o600)

# ── privileged fs via docker exec (volume owned by container uid) ────────────
def cpath(host_path):
    return host_path.replace(HOST_ROOT, CONT_ROOT, 1)

def fs_write(host_path, content: bytes):
    r = subprocess.run(["docker", "exec", "-i", CONTAINER, "sh", "-c", f"cat > '{cpath(host_path)}'"],
                       input=content, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise Exception(r.stderr.decode()[:200])

def fs_remove(host_path):
    r = subprocess.run(["docker", "exec", CONTAINER, "rm", "-f", cpath(host_path)], capture_output=True, timeout=30)
    if r.returncode != 0:
        raise Exception(r.stderr.decode()[:200])

def fs_copy(src_host, dst_host):
    r = subprocess.run(["docker", "exec", CONTAINER, "cp", "-p", cpath(src_host), cpath(dst_host)],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        raise Exception(r.stderr.decode()[:200])

# ── chat/event watcher: tails docker logs → chat buffer + discord webhook ────
chat_buffer = []          # [{ts, kind, player, text}]
chat_lock = threading.Lock()
CHAT_RE = re.compile(r"\[(CHAT|JOIN|LEAVE)\]\s*(.*)")

def discord_notify(kind, text):
    cfg = load_panel_config()
    wh = cfg.get("discord_webhook")
    if not wh or kind not in cfg.get("discord_events", ["join", "leave", "chat"]):
        return
    emoji = {"join": "🟢", "leave": "🔴", "chat": "💬"}.get(kind, "")
    try:
        req = urllib.request.Request(wh, headers={"User-Agent": "factorio-panel", "Content-Type": "application/json"},
                                     data=json.dumps({"content": f"{emoji} {text}"[:1900]}).encode())
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

def chat_watcher():
    while True:
        try:
            proc = subprocess.Popen(["docker", "logs", "-f", "--tail", "0", CONTAINER],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                m = CHAT_RE.search(line)
                if not m:
                    continue
                kind, body = m.group(1).lower(), m.group(2).strip()
                player, text = None, body
                if kind == "chat" and ": " in body:
                    player, text = body.split(": ", 1)
                    if player == "<server>":
                        continue
                elif kind in ("join", "leave"):
                    player = body.split(" ")[0]
                entry = {"ts": datetime.now().strftime("%H:%M:%S"), "kind": kind, "player": player, "text": body}
                with chat_lock:
                    chat_buffer.append(entry)
                    del chat_buffer[:-300]
                discord_notify(kind, body)
            proc.wait()
        except Exception:
            pass
        time.sleep(10)  # container restarted or docker hiccup — reattach

threading.Thread(target=chat_watcher, daemon=True).start()

@app.route("/api/chat")
@login_required
def api_chat():
    since = int(request.args.get("since", 0))
    with chat_lock:
        return jsonify({"ok": True, "total": len(chat_buffer), "entries": chat_buffer[since:]})

# ── metrics ──────────────────────────────────────────────────────────────────
metrics_hist = []  # [{t, players, cpu, mem}]

def sample_metrics():
    while True:
        try:
            if container_status()["state"] == "running":
                players = len(get_players())
                stats, code = run(["docker", "stats", CONTAINER, "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}"], timeout=20)
                cpu = mem = None
                if code == 0 and "|" in stats:
                    c, m = stats.split("|")
                    try: cpu = float(c.replace("%", "").strip())
                    except Exception: pass
                    mem = m.split("/")[0].strip()
                metrics_hist.append({"t": int(time.time()), "players": players, "cpu": cpu, "mem": mem})
                del metrics_hist[:-360]  # 3h at 30s
        except Exception:
            pass
        time.sleep(30)

threading.Thread(target=sample_metrics, daemon=True).start()

@app.route("/api/metrics")
@login_required
def api_metrics():
    ups = None
    try:
        t1 = int(rcon("/silent-command rcon.print(game.tick)"))
        time.sleep(1.0)
        t2 = int(rcon("/silent-command rcon.print(game.tick)"))
        ups = t2 - t1
    except Exception:
        pass
    return jsonify({"ok": True, "ups": ups, "history": metrics_hist[-120:]})

# ── off-box backup (scp to a remote host) ────────────────────────────────────
@app.route("/api/backup-offbox", methods=["POST"])
@role_required("admin")
def api_backup_offbox():
    zips = [f for f in os.listdir(SAVES_DIR) if f.endswith(".zip")]
    if not zips:
        return jsonify({"ok": False, "error": "no saves"}), 404
    newest = max(zips, key=lambda f: os.path.getmtime(os.path.join(SAVES_DIR, f)))
    cfg = load_panel_config()
    dest_host = cfg.get("backup_host") or os.environ.get("BACKUP_HOST", "")
    dest_dir = cfg.get("backup_dir") or os.environ.get("BACKUP_DIR", "~/factorio-backups")
    if not dest_host:
        return jsonify({"ok": False, "error": "no backup_host configured (panel_config.json or BACKUP_HOST env)"}), 400
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out, code = run(["ssh", "-o", "BatchMode=yes", dest_host, f"mkdir -p {dest_dir}"], timeout=30)
    if code != 0:
        return jsonify({"ok": False, "error": f"ssh failed: {out[:120]}"}), 500
    out, code = run(["scp", "-q", os.path.join(SAVES_DIR, newest),
                     f"{dest_host}:{dest_dir}/{ts}_{newest}"], timeout=300)
    audit(f"BACKUP_OFFBOX {newest} ok={code==0}")
    return jsonify({"ok": code == 0, "dest": f"{dest_host}:{dest_dir}/{ts}_{newest}"})

# ── docker helpers ───────────────────────────────────────────────────────────
def run(cmd, timeout=60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.returncode

def container_status():
    out, code = run(["docker", "inspect", CONTAINER, "--format", "{{.State.Status}}|{{.State.StartedAt}}|{{.Config.Image}}"])
    if code != 0:
        return {"state": "absent", "uptime": None, "image": None}
    state, started, image = out.split("|")
    uptime = None
    try:
        dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        d, h, m = secs//86400, secs%86400//3600, secs%3600//60
        uptime = f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m")
    except Exception:
        pass
    return {"state": state, "uptime": uptime, "image": image}

def running_version():
    out, code = run(["docker", "exec", CONTAINER, "cat", "/opt/factorio/data/base/info.json"])
    if code == 0:
        try: return json.loads(out)["version"]
        except Exception: pass
    try:
        m = re.search(r"factorio:([\d.]+)", open(COMPOSE_FILE).read())
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"

def http_json(url, data=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "factorio-panel/2.0"})
    if data is not None:
        req.data = urllib.parse.urlencode(data).encode()
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def latest_versions():
    try:
        data = http_json("https://factorio.com/api/latest-releases")
        pick = lambda d: d if isinstance(d, str) else (d.get("headless") or "")
        return {"stable": pick(data.get("stable", {})), "experimental": pick(data.get("experimental", {}))}
    except Exception:
        return {"stable": None, "experimental": None}

# ── rcon ─────────────────────────────────────────────────────────────────────
def rcon(command):
    pw = open(RCON_PW_FILE).read().strip()
    with socket.socket() as s:
        s.settimeout(6)
        s.connect((RCON_HOST, RCON_PORT))
        def send(rid, rtype, body):
            payload = struct.pack("<ii", rid, rtype) + body.encode() + b"\x00\x00"
            s.sendall(struct.pack("<i", len(payload)) + payload)
        def recvn(n):
            buf = b""
            while len(buf) < n:
                c = s.recv(n - len(buf))
                if not c: raise Exception("rcon closed")
                buf += c
            return buf
        def recv():
            ln = struct.unpack("<i", recvn(4))[0]
            data = recvn(ln)
            rid, rtype = struct.unpack("<ii", data[:8])
            return rid, rtype, data[8:-2].decode(errors="replace")
        send(1, 3, pw)
        rid, _, _ = recv()
        if rid == -1:
            raise Exception("RCON auth failed")
        send(2, 2, command)
        _, _, body = recv()
        return body

def get_players():
    try:
        out = rcon("/players online")
        return [l.strip().split(" (online)")[0] for l in out.splitlines() if "(online)" in l]
    except Exception:
        return []

# ── factorio.com credentials (for mod portal downloads) ──────────────────────
def load_creds():
    if os.path.exists(CREDS_FILE):
        try: return json.load(open(CREDS_FILE))
        except Exception: pass
    return {}

def save_creds(d):
    with open(CREDS_FILE, "w") as f:
        json.dump(d, f)
    os.chmod(CREDS_FILE, 0o600)

# ── config files ─────────────────────────────────────────────────────────────
LIST_FILES = {
    "adminlist": f"{CONFIG_DIR}/server-adminlist.json",
    "banlist": f"{CONFIG_DIR}/server-banlist.json",
    "whitelist": f"{CONFIG_DIR}/server-whitelist.json",
}

def read_json_file(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

# ── mods ─────────────────────────────────────────────────────────────────────
BUILTIN_MODS = {"base", "space-age", "elevated-rails", "quality"}

def mod_files():
    """{name: (filename, version)} from mods dir zips."""
    out = {}
    if not os.path.isdir(MODS_DIR):
        return out
    for f in os.listdir(MODS_DIR):
        m = re.fullmatch(r"(.+)_(\d+\.\d+\.\d+)\.zip", f)
        if m:
            name, ver = m.group(1), m.group(2)
            if name not in out or tuple(map(int, ver.split("."))) > tuple(map(int, out[name][1].split("."))):
                out[name] = (f, ver)
    return out

def load_mod_list():
    return read_json_file(f"{MODS_DIR}/mod-list.json", {"mods": [{"name": "base", "enabled": True}]})

def save_mod_list(data):
    fs_write(f"{MODS_DIR}/mod-list.json", json.dumps(data, indent=2).encode())

def portal_info(name):
    return http_json(f"https://mods.factorio.com/api/mods/{urllib.parse.quote(name)}/full", timeout=20)

def best_release(info, game_version):
    """Pick newest release compatible with the running game (major.minor match, fallback latest)."""
    gv = ".".join(game_version.split(".")[:2])
    rels = info.get("releases", [])
    compat = [r for r in rels if r.get("info_json", {}).get("factorio_version") == gv]
    pool = compat or rels
    return pool[-1] if pool else None

def download_mod(release):
    creds = load_creds()
    if not creds.get("username") or not creds.get("token"):
        raise Exception("factorio.com credentials not set (Mods tab → Credentials)")
    url = ("https://mods.factorio.com" + release["download_url"] +
           f"?username={urllib.parse.quote(creds['username'])}&token={urllib.parse.quote(creds['token'])}")
    req = urllib.request.Request(url, headers={"User-Agent": "factorio-panel/2.0"})
    data = urllib.request.urlopen(req, timeout=120).read()
    if data[:2] != b"PK":
        raise Exception("download failed — check credentials (got non-zip response)")
    fs_write(f"{MODS_DIR}/{release['file_name']}", data)
    return release["file_name"]

# ── routes: auth/basic ───────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        time.sleep(0.5)
        username = (request.form.get("username") or "").strip()
        pw = request.form.get("password", "")
        u = find_user(username) if username else None
        if u and verify_pw(pw, u["pw"]):
            session.update(auth=True, username=u["username"], role=u["role"])
            audit(f"LOGIN ok user={u['username']}")
            return redirect(url_for("index"))
        # legacy bootstrap: no users yet + master password → become admin
        if not load_users() and check_legacy_password(pw):
            name = username or "admin"
            save_users([{"username": name, "pw": hash_pw(pw), "role": "admin"}])
            session.update(auth=True, username=name, role="admin")
            audit(f"LOGIN legacy-bootstrap -> created admin {name}")
            return redirect(url_for("index"))
        audit(f"LOGIN failed user={username!r}")
        flash("Wrong username or password.")
    return render_template_string(LOGIN_HTML)

@app.route("/api/me")
@login_required
def api_me():
    return jsonify({"ok": True, "username": session.get("username"), "role": current_role()})

@app.route("/api/users", methods=["GET", "POST"])
@role_required("admin")
def api_users():
    if request.method == "GET":
        return jsonify({"ok": True, "users": [{"username": u["username"], "role": u["role"]} for u in load_users()]})
    d = request.json or {}
    action = d.get("action")
    uname = (d.get("username") or "").strip()
    users = load_users()
    if action == "add":
        if not re.fullmatch(r"[A-Za-z0-9_.-]{2,30}", uname):
            return jsonify({"ok": False, "error": "bad username"}), 400
        if find_user(uname):
            return jsonify({"ok": False, "error": "exists"}), 409
        role = d.get("role", "viewer")
        pw = d.get("password", "")
        if role not in ROLES or len(pw) < 8:
            return jsonify({"ok": False, "error": "role invalid or password under 8 chars"}), 400
        users.append({"username": uname, "pw": hash_pw(pw), "role": role})
        save_users(users)
        audit(f"USER_ADD {uname} role={role}")
        return jsonify({"ok": True})
    if action == "delete":
        if uname.lower() == session.get("username", "").lower():
            return jsonify({"ok": False, "error": "cannot delete yourself"}), 400
        n = [u for u in users if u["username"].lower() != uname.lower()]
        if len(n) == len(users):
            return jsonify({"ok": False, "error": "not found"}), 404
        admins = [u for u in n if u["role"] == "admin"]
        if not admins:
            return jsonify({"ok": False, "error": "would leave no admin"}), 400
        save_users(n)
        audit(f"USER_DELETE {uname}")
        return jsonify({"ok": True})
    if action == "setrole":
        role = d.get("role")
        if role not in ROLES:
            return jsonify({"ok": False, "error": "bad role"}), 400
        u = find_user(uname)
        if not u:
            return jsonify({"ok": False, "error": "not found"}), 404
        for x in users:
            if x["username"].lower() == uname.lower(): x["role"] = role
        if not any(x["role"] == "admin" for x in users):
            return jsonify({"ok": False, "error": "would leave no admin"}), 400
        save_users(users)
        audit(f"USER_SETROLE {uname} -> {role}")
        return jsonify({"ok": True})
    if action == "setpassword":
        pw = d.get("password", "")
        target = uname or session.get("username")
        if target.lower() != session.get("username", "").lower() and current_role() != "admin":
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if len(pw) < 8:
            return jsonify({"ok": False, "error": "password under 8 chars"}), 400
        for x in users:
            if x["username"].lower() == target.lower(): x["pw"] = hash_pw(pw)
        save_users(users)
        audit(f"USER_SETPW {target}")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "bad action"}), 400

@app.route("/api/panelconfig", methods=["GET", "POST"])
@role_required("admin")
def api_panelconfig():
    cfg = load_panel_config()
    if request.method == "GET":
        return jsonify({"ok": True, "discord_webhook_set": bool(cfg.get("discord_webhook")),
                        "discord_events": cfg.get("discord_events", ["join", "leave", "chat"])})
    d = request.json or {}
    if "discord_webhook" in d:
        wh = (d.get("discord_webhook") or "").strip()
        if wh and not wh.startswith("https://discord.com/api/webhooks/"):
            return jsonify({"ok": False, "error": "not a discord webhook URL"}), 400
        cfg["discord_webhook"] = wh
    if "discord_events" in d and isinstance(d["discord_events"], list):
        cfg["discord_events"] = [e for e in d["discord_events"] if e in ("join", "leave", "chat")]
    save_panel_config(cfg)
    audit("PANELCONFIG saved")
    return jsonify({"ok": True})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    return render_template_string(PANEL_HTML)

@app.route("/api/status")
@login_required
def api_status():
    c = container_status()
    players = get_players() if c["state"] == "running" else []
    return jsonify({"container": c, "version": running_version() if c["state"] == "running" else None,
                    "players": players, "countdown": control_state})

@app.route("/api/versions")
@login_required
def api_versions():
    return jsonify({"running": running_version(), **latest_versions()})

@app.route("/api/control", methods=["POST"])
@role_required("moderator")
def api_control():
    action = (request.json or {}).get("action")
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "bad action"}), 400
    if action == "start":
        _, code = run(["docker", "start", CONTAINER])
        audit(f"START ok={code==0}")
        return jsonify({"ok": code == 0})
    if control_lock.locked():
        return jsonify({"ok": False, "error": "countdown already running"}), 409
    players = get_players()
    def do():
        with control_lock:
            if players:
                control_state.update(countdown=True, action=action)
                steps = [(60, 30), (30, 20), (10, 5), (5, 5)]
                for s_, delay in steps:
                    control_state["seconds_left"] = s_
                    try: rcon(f"/silent-command game.print('[Merlin] Server {action} in {s_} seconds')")
                    except Exception: pass
                    time.sleep(delay)
                control_state.update(countdown=False, seconds_left=0, action=None)
            run(["docker", action, CONTAINER], timeout=240)
            audit(f"{action.upper()} players_were_online={len(players)}")
    threading.Thread(target=do, daemon=True).start()
    return jsonify({"ok": True, "countdown": bool(players)})

@app.route("/api/say", methods=["POST"])
@role_required("moderator")
def api_say():
    msg = (request.json or {}).get("message", "").strip()[:200].replace("'", "\\'")
    if not msg:
        return jsonify({"ok": False}), 400
    try:
        rcon(f"/silent-command game.print('[Panel] {msg}')")
        audit(f"SAY {msg!r}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/logs")
@login_required
def api_logs():
    out, _ = run(["docker", "logs", "--tail", "100", CONTAINER])
    return jsonify({"logs": out.splitlines()[-100:]})

@app.route("/api/liveplayers")
@login_required
def api_liveplayers():
    lua = ("/silent-command local t={} for _,p in pairs(game.connected_players) do "
           "t[#t+1]={name=p.name,x=math.floor(p.position.x),y=math.floor(p.position.y),"
           "surface=p.surface.name,online=math.floor(p.online_time/3600),afk=math.floor(p.afk_time/3600),"
           "admin=p.admin} end rcon.print(helpers.table_to_json(t))")
    try:
        out = rcon(lua)
        players = json.loads(out) if out.strip() else []
        if isinstance(players, dict):  # lua {} → {} when empty
            players = []
        return jsonify({"ok": True, "players": players})
    except Exception as e:
        return jsonify({"ok": True, "players": [], "note": str(e)[:80]})

# ── routes: console & player admin ───────────────────────────────────────────
@app.route("/api/rcon", methods=["POST"])
@role_required("moderator")
def api_rcon():
    cmd = (request.json or {}).get("command", "").strip()
    if not cmd:
        return jsonify({"ok": False, "error": "empty"}), 400
    try:
        out = rcon(cmd)
        audit(f"RCON {cmd!r}")
        return jsonify({"ok": True, "output": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/player-action", methods=["POST"])
@role_required("moderator")
def api_player_action():
    d = request.json or {}
    action, player = d.get("action"), (d.get("player") or "").strip()
    reason = (d.get("reason") or "").strip()
    cmds = {"kick": f"/kick {player} {reason}".strip(), "ban": f"/ban {player} {reason}".strip(),
            "unban": f"/unban {player}", "promote": f"/promote {player}", "demote": f"/demote {player}",
            "mute": f"/mute {player}", "unmute": f"/unmute {player}"}
    if action not in cmds or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", player):
        return jsonify({"ok": False, "error": "bad action/player"}), 400
    try:
        out = rcon(cmds[action])
        audit(f"PLAYER_{action.upper()} {player} {reason!r}")
        return jsonify({"ok": True, "output": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── routes: lists (admin/ban/white) ──────────────────────────────────────────
@app.route("/api/list/<kind>", methods=["GET", "POST"])
@role_required("moderator")
def api_list(kind):
    if kind not in LIST_FILES:
        return jsonify({"ok": False}), 404
    path = LIST_FILES[kind]
    if request.method == "GET":
        return jsonify({"ok": True, "entries": read_json_file(path, [])})
    entries = (request.json or {}).get("entries")
    if not isinstance(entries, list) or not all(isinstance(x, (str, dict)) for x in entries):
        return jsonify({"ok": False, "error": "entries must be a list"}), 400
    try:
        fs_write(path, json.dumps(entries, indent=2).encode())
        audit(f"LIST_{kind.upper()} saved n={len(entries)}")
        return jsonify({"ok": True, "note": "restart server to apply" if kind != "banlist" else "applied on restart; use Players tab ban for live bans"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── settings schema (forms UI) ───────────────────────────────────────────────
SETTINGS_SCHEMA = [
 {"section": "General", "fields": [
   {"key": "name", "label": "Server name", "type": "str", "help": "Shown in the server browser"},
   {"key": "description", "label": "Description", "type": "str"},
   {"key": "tags", "label": "Tags", "type": "strlist", "help": "comma-separated"},
   {"key": "max_players", "label": "Max players", "type": "int", "min": 0, "max": 500, "help": "0 = unlimited; admins can always join"},
 ]},
 {"section": "Visibility & Access", "fields": [
   {"key": "visibility.public", "label": "Public (listed on matching server)", "type": "bool"},
   {"key": "visibility.lan", "label": "LAN visible", "type": "bool"},
   {"key": "game_password", "label": "Game password", "type": "secret", "help": "empty = no password"},
   {"key": "require_user_verification", "label": "Require valid factorio.com account", "type": "bool"},
   {"key": "ignore_player_limit_for_returning_players", "label": "Returning players bypass player limit", "type": "bool"},
   {"key": "username", "label": "factorio.com username (for public listing)", "type": "str"},
   {"key": "token", "label": "factorio.com token (for public listing)", "type": "secret"},
 ]},
 {"section": "Gameplay & Admin", "fields": [
   {"key": "allow_commands", "label": "Allow console commands", "type": "enum", "choices": ["true", "false", "admins-only"]},
   {"key": "only_admins_can_pause_the_game", "label": "Only admins can pause", "type": "bool"},
   {"key": "auto_pause", "label": "Pause when empty", "type": "bool"},
   {"key": "auto_pause_when_players_connect", "label": "Pause while players connect", "type": "bool"},
   {"key": "afk_autokick_interval", "label": "AFK autokick (minutes)", "type": "int", "min": 0, "max": 1440, "help": "0 = never"},
 ]},
 {"section": "Autosave", "fields": [
   {"key": "autosave_interval", "label": "Autosave interval (minutes)", "type": "int", "min": 1, "max": 240},
   {"key": "autosave_slots", "label": "Autosave slots", "type": "int", "min": 1, "max": 50},
   {"key": "autosave_only_on_server", "label": "Autosave only on server", "type": "bool"},
   {"key": "non_blocking_saving", "label": "Non-blocking saving (experimental)", "type": "bool", "help": "risk of save corruption — Factorio's warning, not mine"},
 ]},
 {"section": "Network (advanced)", "fields": [
   {"key": "max_heartbeats_per_second", "label": "Max heartbeats/sec (tick rate)", "type": "int", "min": 6, "max": 240},
   {"key": "max_upload_in_kilobytes_per_second", "label": "Max upload KB/s", "type": "int", "min": 0, "max": 1000000, "help": "0 = unlimited"},
   {"key": "max_upload_slots", "label": "Max upload slots", "type": "int", "min": 0, "max": 64, "help": "0 = unlimited"},
   {"key": "minimum_latency_in_ticks", "label": "Minimum latency (ticks)", "type": "int", "min": 0, "max": 120, "help": "1 tick = 16ms"},
 ]},
]

def _get_path(d, path):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur

def _set_path(d, path, value):
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value

def validate_field(f, raw):
    t = f["type"]
    if t == "bool":
        if isinstance(raw, bool): return raw
        raise ValueError(f"{f['key']}: expected boolean")
    if t == "int":
        try: v = int(raw)
        except Exception: raise ValueError(f"{f['key']}: not an integer")
        if "min" in f and v < f["min"]: raise ValueError(f"{f['key']}: below minimum {f['min']}")
        if "max" in f and v > f["max"]: raise ValueError(f"{f['key']}: above maximum {f['max']}")
        return v
    if t == "enum":
        if raw not in f["choices"]: raise ValueError(f"{f['key']}: must be one of {f['choices']}")
        return raw
    if t == "strlist":
        if isinstance(raw, list): return [str(x).strip() for x in raw if str(x).strip()]
        return [s.strip() for s in str(raw).split(",") if s.strip()]
    return str(raw)

@app.route("/api/settings-form", methods=["GET", "POST"])
@role_required("admin")
def api_settings_form():
    path = f"{CONFIG_DIR}/server-settings.json"
    current = read_json_file(path, {})
    if request.method == "GET":
        out = []
        for sec in SETTINGS_SCHEMA:
            fields = []
            for f in sec["fields"]:
                v = _get_path(current, f["key"])
                fv = v if f["type"] != "secret" else ("__SET__" if v else "")
                if f["type"] == "strlist" and isinstance(v, list):
                    fv = ", ".join(v)
                fields.append({**{k: f.get(k) for k in ("key","label","type","help","min","max","choices")}, "value": fv})
            out.append({"section": sec["section"], "fields": fields})
        return jsonify({"ok": True, "sections": out})
    # POST: validate + merge
    changes = (request.json or {}).get("values", {})
    schema_by_key = {f["key"]: f for sec in SETTINGS_SCHEMA for f in sec["fields"]}
    errors, staged = [], []
    for key, raw in changes.items():
        f = schema_by_key.get(key)
        if not f:
            errors.append(f"unknown field {key}"); continue
        if f["type"] == "secret" and raw == "__SET__":
            continue  # untouched masked secret
        try:
            staged.append((key, validate_field(f, raw)))
        except ValueError as e:
            errors.append(str(e))
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    for key, v in staged:
        _set_path(current, key, v)
    try:
        fs_copy(path, path + ".bak")
        fs_write(path, json.dumps(current, indent=2).encode())
        audit(f"SETTINGS_FORM saved fields={[k for k,_ in staged]}")
        return jsonify({"ok": True, "note": "restart server to apply"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── routes: server settings ──────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET", "POST"])
@role_required("admin")
def api_settings():
    path = f"{CONFIG_DIR}/server-settings.json"
    if request.method == "GET":
        return jsonify({"ok": True, "settings": read_json_file(path, {})})
    try:
        new = request.json.get("settings")
        if not isinstance(new, dict):
            return jsonify({"ok": False, "error": "settings must be an object"}), 400
        fs_copy(path, path + ".bak")
        fs_write(path, json.dumps(new, indent=2).encode())
        audit("SETTINGS saved")
        return jsonify({"ok": True, "note": "restart server to apply"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── routes: saves ────────────────────────────────────────────────────────────
@app.route("/api/saves")
@login_required
def api_saves():
    saves = []
    for f in sorted(os.listdir(SAVES_DIR)):
        if f.endswith(".zip"):
            st = os.stat(os.path.join(SAVES_DIR, f))
            saves.append({"name": f, "size_mb": round(st.st_size/1048576, 1),
                          "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    saves.sort(key=lambda s: s["mtime"], reverse=True)
    return jsonify({"saves": saves})

@app.route("/api/saves/backup", methods=["POST"])
@role_required("moderator")
def api_backup():
    zips = [f for f in os.listdir(SAVES_DIR) if f.endswith(".zip") and not f.startswith("panelbackup_")]
    if not zips:
        return jsonify({"ok": False, "error": "no saves"}), 404
    newest = max(zips, key=lambda f: os.path.getmtime(os.path.join(SAVES_DIR, f)))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = f"panelbackup_{ts}_{newest}"
    try:
        fs_copy(os.path.join(SAVES_DIR, newest), os.path.join(SAVES_DIR, dest))
        audit(f"BACKUP {dest}")
        return jsonify({"ok": True, "name": dest})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/saves/upload", methods=["POST"])
@role_required("moderator")
def api_saves_upload():
    f = request.files.get("file")
    if not f or not f.filename.endswith(".zip"):
        return jsonify({"ok": False, "error": "need a .zip"}), 400
    name = os.path.basename(f.filename)
    data = f.read()
    if data[:2] != b"PK":
        return jsonify({"ok": False, "error": "not a zip"}), 400
    try:
        fs_write(os.path.join(SAVES_DIR, name), data)
        audit(f"SAVE_UPLOAD {name} {len(data)//1024}KB")
        return jsonify({"ok": True, "name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/saves/delete", methods=["POST"])
@role_required("moderator")
def api_saves_delete():
    name = os.path.basename((request.json or {}).get("name", ""))
    if not name.endswith(".zip"):
        return jsonify({"ok": False}), 400
    path = os.path.join(SAVES_DIR, name)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        fs_remove(path)
        audit(f"SAVE_DELETE {name}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/saves/download/<name>")
@login_required
def download_save(name):
    name = os.path.basename(name)
    path = os.path.join(SAVES_DIR, name)
    if not os.path.exists(path) or not name.endswith(".zip"):
        return "not found", 404
    audit(f"DOWNLOAD {name}")
    return send_file(path, as_attachment=True)

# ── world settings (map-settings.json) + map gen + boot save ─────────────────
WORLD_SCHEMA = [
 {"section": "Difficulty", "file": "map-settings", "fields": [
   {"key": "difficulty_settings.technology_price_multiplier", "label": "Technology price multiplier", "type": "float", "min": 0.1, "max": 1000, "help": "research cost scale; 1 = normal"},
   {"key": "difficulty_settings.spoil_time_modifier", "label": "Spoil time modifier", "type": "float", "min": 0.1, "max": 100, "help": "Space Age spoilage speed; higher = slower spoil"},
 ]},
 {"section": "Enemy Evolution", "file": "map-settings", "fields": [
   {"key": "enemy_evolution.enabled", "label": "Evolution enabled", "type": "bool"},
   {"key": "enemy_evolution.time_factor", "label": "Time factor", "type": "float", "min": 0, "max": 0.01, "help": "evolution per tick from time; default 0.000004"},
   {"key": "enemy_evolution.destroy_factor", "label": "Destroy factor", "type": "float", "min": 0, "max": 1, "help": "evolution per spawner destroyed; default 0.002"},
   {"key": "enemy_evolution.pollution_factor", "label": "Pollution factor", "type": "float", "min": 0, "max": 0.001, "help": "evolution per pollution unit; default 0.0000009"},
 ]},
 {"section": "Enemy Expansion", "file": "map-settings", "fields": [
   {"key": "enemy_expansion.enabled", "label": "Expansion enabled", "type": "bool", "help": "biters settle new nests"},
   {"key": "enemy_expansion.max_expansion_distance", "label": "Max expansion distance (chunks)", "type": "int", "min": 2, "max": 20},
   {"key": "enemy_expansion.settler_group_min_size", "label": "Settler group min size", "type": "int", "min": 1, "max": 50},
   {"key": "enemy_expansion.settler_group_max_size", "label": "Settler group max size", "type": "int", "min": 1, "max": 100},
   {"key": "enemy_expansion.min_expansion_cooldown", "label": "Min cooldown (ticks)", "type": "int", "min": 3600, "max": 216000, "help": "3600 ticks = 1 min"},
   {"key": "enemy_expansion.max_expansion_cooldown", "label": "Max cooldown (ticks)", "type": "int", "min": 3600, "max": 864000},
 ]},
 {"section": "Pollution", "file": "map-settings", "fields": [
   {"key": "pollution.enabled", "label": "Pollution enabled", "type": "bool"},
   {"key": "pollution.diffusion_ratio", "label": "Diffusion ratio", "type": "float", "min": 0, "max": 0.25, "help": "spread to neighbor chunks; default 0.02"},
   {"key": "pollution.ageing", "label": "Absorption rate (ageing)", "type": "float", "min": 0, "max": 10, "help": "default 1"},
   {"key": "pollution.enemy_attack_pollution_consumption_modifier", "label": "Attack pollution consumption", "type": "float", "min": 0.1, "max": 10, "help": "higher = fewer attacks per pollution"},
 ]},
]

MAPGEN_SCHEMA = [
 {"section": "World", "fields": [
   {"key": "seed", "label": "Seed", "type": "int_or_null", "help": "empty = random"},
   {"key": "width", "label": "Width (0 = infinite)", "type": "int", "min": 0, "max": 2000000},
   {"key": "height", "label": "Height (0 = infinite)", "type": "int", "min": 0, "max": 2000000},
   {"key": "starting_area", "label": "Starting area size", "type": "float", "min": 0.17, "max": 6, "help": "1 = normal, larger = safer start"},
   {"key": "peaceful_mode", "label": "Peaceful mode", "type": "bool", "help": "biters only attack when provoked"},
 ]},
 {"section": "Resources (frequency / size / richness)", "fields": [
   {"key": f"autoplace_controls.{ore}.{prop}", "label": f"{ore} {prop}", "type": "float", "min": 0, "max": 6}
   for ore in ("iron-ore", "copper-ore", "coal", "stone", "crude-oil", "uranium-ore")
   for prop in ("frequency", "size", "richness")
 ]},
 {"section": "Terrain & Enemies", "fields": [
   {"key": "autoplace_controls.water.frequency", "label": "water frequency", "type": "float", "min": 0, "max": 6},
   {"key": "autoplace_controls.water.size", "label": "water size", "type": "float", "min": 0, "max": 6},
   {"key": "autoplace_controls.trees.frequency", "label": "trees frequency", "type": "float", "min": 0, "max": 6},
   {"key": "autoplace_controls.trees.size", "label": "trees size", "type": "float", "min": 0, "max": 6},
   {"key": "autoplace_controls.enemy-base.frequency", "label": "enemy bases frequency", "type": "float", "min": 0, "max": 6},
   {"key": "autoplace_controls.enemy-base.size", "label": "enemy bases size", "type": "float", "min": 0, "max": 6},
   {"key": "cliff_settings.richness", "label": "cliff continuity (0 = no cliffs)", "type": "float", "min": 0, "max": 10},
 ]},
]

def validate_field2(f, raw):
    t = f["type"]
    if t == "float":
        try: v = float(raw)
        except Exception: raise ValueError(f"{f['key']}: not a number")
        if "min" in f and v < f["min"]: raise ValueError(f"{f['key']}: below {f['min']}")
        if "max" in f and v > f["max"]: raise ValueError(f"{f['key']}: above {f['max']}")
        return v
    if t == "int_or_null":
        if raw in ("", None, "null"): return None
        try: return int(raw)
        except Exception: raise ValueError(f"{f['key']}: not an integer")
    return validate_field(f, raw)

def schema_form_response(schema, current):
    out = []
    for sec in schema:
        fields = []
        for f in sec["fields"]:
            v = _get_path(current, f["key"])
            fields.append({**{k: f.get(k) for k in ("key","label","type","help","min","max","choices")}, "value": v})
        out.append({"section": sec["section"], "fields": fields})
    return out

def schema_save(schema, current, changes):
    schema_by_key = {f["key"]: f for sec in schema for f in sec["fields"]}
    errors, staged = [], []
    for key, raw in changes.items():
        f = schema_by_key.get(key)
        if not f:
            errors.append(f"unknown field {key}"); continue
        try:
            staged.append((key, validate_field2(f, raw)))
        except ValueError as e:
            errors.append(str(e))
    return errors, staged

@app.route("/api/world", methods=["GET", "POST"])
@role_required("admin")
def api_world():
    path = f"{CONFIG_DIR}/map-settings.json"
    current = read_json_file(path, {})
    if request.method == "GET":
        return jsonify({"ok": True, "sections": schema_form_response(WORLD_SCHEMA, current)})
    errors, staged = schema_save(WORLD_SCHEMA, current, (request.json or {}).get("values", {}))
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    for k, v in staged: _set_path(current, k, v)
    try:
        fs_copy(path, path + ".bak")
        fs_write(path, json.dumps(current, indent=2).encode())
        audit(f"WORLD saved fields={[k for k,_ in staged]}")
        return jsonify({"ok": True, "note": "applies to the current map after server restart"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/mapgen", methods=["GET", "POST"])
@role_required("admin")
def api_mapgen():
    path = f"{CONFIG_DIR}/map-gen-settings.json"
    current = read_json_file(path, {})
    if request.method == "GET":
        return jsonify({"ok": True, "sections": schema_form_response(MAPGEN_SCHEMA, current)})
    errors, staged = schema_save(MAPGEN_SCHEMA, current, (request.json or {}).get("values", {}))
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    for k, v in staged: _set_path(current, k, v)
    try:
        fs_copy(path, path + ".bak")
        fs_write(path, json.dumps(current, indent=2).encode())
        audit(f"MAPGEN saved fields={[k for k,_ in staged]}")
        return jsonify({"ok": True, "note": "used when generating a NEW map"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/newmap", methods=["POST"])
@role_required("admin")
def api_newmap():
    name = (request.json or {}).get("name", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{2,60}", name):
        return jsonify({"ok": False, "error": "bad name (letters/numbers/spaces/-_.)"}), 400
    fname = name.replace(" ", "-") + ".zip"
    if os.path.exists(os.path.join(SAVES_DIR, fname)):
        return jsonify({"ok": False, "error": "a save with that name exists"}), 409
    m = re.search(r"factoriotools/factorio:([\d.]+)", open(COMPOSE_FILE).read())
    tag = m.group(1) if m else "stable"
    was_running = container_status()["state"] == "running"
    def do():
        run(["docker", "compose", "-f", COMPOSE_FILE, "down"], timeout=180)
        run(["docker", "run", "--rm", "-v", "/opt/factorio:/factorio",
             "--entrypoint", "/opt/factorio/bin/x64/factorio", f"factoriotools/factorio:{tag}",
             "--create", f"/factorio/saves/{fname}",
             "--map-gen-settings", "/factorio/config/map-gen-settings.json",
             "--map-settings", "/factorio/config/map-settings.json"], timeout=300)
        run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d"], timeout=300)
        audit(f"NEWMAP {fname} tag={tag}")
    threading.Thread(target=do, daemon=True).start()
    return jsonify({"ok": True, "file": fname,
                    "note": "generating; server restarts and loads the newest save (= the new map)"})

@app.route("/api/bootsave", methods=["GET", "POST"])
@role_required("admin")
def api_bootsave():
    txt = open(COMPOSE_FILE).read()
    m = re.search(r"SAVE_NAME=([^\s\"]+)", txt)
    if request.method == "GET":
        return jsonify({"ok": True, "mode": "specific" if m else "latest",
                        "save": m.group(1) if m else None})
    target = (request.json or {}).get("save", "").strip()  # "" = latest
    new = re.sub(r"\n\s*- SAVE_NAME=[^\n]*", "", txt)
    if target:
        base = os.path.basename(target)
        if not os.path.exists(os.path.join(SAVES_DIR, base)):
            return jsonify({"ok": False, "error": "save not found"}), 404
        stem = base[:-4] if base.endswith(".zip") else base
        new = new.replace("- LOAD_LATEST_SAVE=true", f"- LOAD_LATEST_SAVE=false\n      - SAVE_NAME={stem}")
    else:
        new = new.replace("- LOAD_LATEST_SAVE=false", "- LOAD_LATEST_SAVE=true")
    open(COMPOSE_FILE, "w").write(new)
    out, code = run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d"], timeout=300)
    audit(f"BOOTSAVE {'latest' if not target else target} ok={code==0}")
    return jsonify({"ok": code == 0, "note": "server recreated with new boot save policy"})

# ── routes: version update ───────────────────────────────────────────────────
@app.route("/api/update", methods=["POST"])
@role_required("admin")
def api_update():
    version = (request.json or {}).get("version", "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return jsonify({"ok": False, "error": "version must be like 2.1.17"}), 400
    api_backup()
    txt = open(COMPOSE_FILE).read()
    new = re.sub(r"factoriotools/factorio:[\d.]+", f"factoriotools/factorio:{version}", txt)
    open(COMPOSE_FILE, "w").write(new)
    out, code = run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--pull", "always"], timeout=600)
    audit(f"UPDATE -> {version} ok={code==0}")
    return jsonify({"ok": code == 0, "output": out[-500:]})

# ── routes: mods ─────────────────────────────────────────────────────────────
@app.route("/api/mods")
@login_required
def api_mods():
    ml = load_mod_list()
    files = mod_files()
    mods = []
    for m in ml.get("mods", []):
        name = m["name"]
        f = files.get(name)
        mods.append({"name": name, "enabled": bool(m.get("enabled")),
                     "version": f[1] if f else None, "file": f[0] if f else None,
                     "builtin": name in BUILTIN_MODS})
    listed = {m["name"] for m in mods}
    for name, (fn, ver) in files.items():
        if name not in listed:
            mods.append({"name": name, "enabled": False, "version": ver, "file": fn, "builtin": False, "unlisted": True})
    creds = load_creds()
    return jsonify({"mods": mods, "creds_set": bool(creds.get("token"))})

@app.route("/api/mods/toggle", methods=["POST"])
@role_required("admin")
def api_mods_toggle():
    name = (request.json or {}).get("name", "")
    ml = load_mod_list()
    found = False
    for m in ml["mods"]:
        if m["name"] == name:
            m["enabled"] = not m.get("enabled")
            found = True
            state = m["enabled"]
    if not found:
        ml["mods"].append({"name": name, "enabled": True})
        state = True
    try:
        save_mod_list(ml)
        audit(f"MOD_TOGGLE {name} -> {state}")
        return jsonify({"ok": True, "enabled": state, "note": "restart server to apply"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/mods/remove", methods=["POST"])
@role_required("admin")
def api_mods_remove():
    name = (request.json or {}).get("name", "")
    if name in BUILTIN_MODS:
        return jsonify({"ok": False, "error": "builtin"}), 400
    files = mod_files()
    try:
        if name in files:
            fs_remove(os.path.join(MODS_DIR, files[name][0]))
        ml = load_mod_list()
        ml["mods"] = [m for m in ml["mods"] if m["name"] != name]
        save_mod_list(ml)
        audit(f"MOD_REMOVE {name}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/mods/search")
@login_required
def api_mods_search():
    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify({"ok": False}), 400
    try:
        results = search_mod_index(q)
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

_mod_index = {"ts": 0, "data": None, "lock": threading.Lock()}

def search_mod_index(q):
    """The portal API has no server-side search; fetch the full list (cached 1h) and filter."""
    with _mod_index["lock"]:
        if not _mod_index["data"] or time.time() - _mod_index["ts"] > 3600:
            data = http_json("https://mods.factorio.com/api/mods?page_size=max", timeout=60)
            _mod_index["data"] = data.get("results", [])
            _mod_index["ts"] = time.time()
    hits = [r for r in _mod_index["data"]
            if q in r["name"].lower() or q in (r.get("title") or "").lower()]
    hits.sort(key=lambda r: -(r.get("downloads_count") or 0))
    return [{"name": r["name"], "title": r.get("title"), "owner": r.get("owner"),
             "downloads": r.get("downloads_count"), "summary": (r.get("summary") or "")[:140],
             "latest": (r.get("latest_release") or {}).get("version")}
            for r in hits[:12]]

def _install_one(name, gv, seen):
    """Install a mod and recursively its required deps. Returns list of installed (name, version)."""
    if name in seen or name in BUILTIN_MODS or name == "base":
        return []
    seen.add(name)
    info = portal_info(name)
    rel = best_release(info, gv)
    if not rel:
        raise Exception(f"no release for {name}")
    download_mod(rel)
    installed = [(name, rel["version"])]
    ml = load_mod_list()
    if not any(m["name"] == name for m in ml["mods"]):
        ml["mods"].append({"name": name, "enabled": True})
    else:
        for m in ml["mods"]:
            if m["name"] == name: m["enabled"] = True
    save_mod_list(ml)
    for dep in rel.get("info_json", {}).get("dependencies", []):
        dep = dep.strip()
        if dep.startswith(("?", "!", "~", "(?)")):  # optional/incompat/hidden — skip
            continue
        dep_name = re.split(r"[<>=]", dep)[0].strip()
        if dep_name in ("base",) or dep_name in BUILTIN_MODS:
            continue
        installed += _install_one(dep_name, gv, seen)
    return installed

@app.route("/api/mods/install", methods=["POST"])
@role_required("admin")
def api_mods_install():
    name = (request.json or {}).get("name", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_ .!'-]{2,80}", name):
        return jsonify({"ok": False, "error": "bad name"}), 400
    try:
        installed = _install_one(name, running_version(), set())
        audit(f"MOD_INSTALL {installed}")
        return jsonify({"ok": True, "installed": [{"name": n, "version": v} for n, v in installed],
                        "note": "restart server to apply"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/mods/check-updates")
@login_required
def api_mods_check_updates():
    files = mod_files()
    gv = running_version()
    out = []
    for name, (fn, ver) in files.items():
        if name in BUILTIN_MODS:
            continue
        try:
            info = portal_info(name)
            rel = best_release(info, gv)
            if rel and rel["version"] != ver:
                out.append({"name": name, "installed": ver, "available": rel["version"]})
        except Exception:
            pass
    return jsonify({"ok": True, "updates": out})

@app.route("/api/mods/update", methods=["POST"])
@role_required("admin")
def api_mods_update():
    name = (request.json or {}).get("name", "").strip()
    files = mod_files()
    if name not in files:
        return jsonify({"ok": False, "error": "not installed"}), 404
    try:
        info = portal_info(name)
        rel = best_release(info, running_version())
        if not rel:
            return jsonify({"ok": False, "error": "no release"}), 404
        old_fn = files[name][0]
        fn = download_mod(rel)
        if fn != old_fn:
            fs_remove(os.path.join(MODS_DIR, old_fn))
        audit(f"MOD_UPDATE {name} -> {rel['version']}")
        return jsonify({"ok": True, "version": rel["version"], "note": "restart server to apply"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/credentials", methods=["GET", "POST"])
@role_required("admin")
def api_credentials():
    if request.method == "GET":
        c = load_creds()
        return jsonify({"ok": True, "username": c.get("username"), "token_set": bool(c.get("token"))})
    d = request.json or {}
    username = (d.get("username") or "").strip()
    token = (d.get("token") or "").strip()
    password = d.get("password") or ""
    if username and password and not token:
        try:
            resp = http_json("https://auth.factorio.com/api-login",
                             {"username": username, "password": password, "api_version": "6"})
            token = resp.get("token") if isinstance(resp, dict) else resp[0]
        except Exception as e:
            return jsonify({"ok": False, "error": f"login failed: {e}"}), 400
    if not username or not token:
        return jsonify({"ok": False, "error": "need username + token (or password)"}), 400
    save_creds({"username": username, "token": token})
    audit(f"CREDS set for {username}")
    return jsonify({"ok": True})

# ── HTML ─────────────────────────────────────────────────────────────────────
LOGIN_HTML = """<!doctype html><html><head><title>Factorio Panel</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{background:#1a1a24;color:#e8e8f0;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
form{background:#242433;padding:2rem;border-radius:12px;box-shadow:0 4px 24px #0008}input{padding:.6rem;border-radius:8px;border:1px solid #444;background:#1a1a24;color:#fff;width:220px}
button{margin-top:.8rem;padding:.6rem 1.2rem;border-radius:8px;border:0;background:#e8902a;color:#fff;font-weight:600;cursor:pointer;width:100%}
h2{margin-top:0}.f{color:#f66;font-size:.85rem}</style></head><body>
<form method=post><h2>⚙️ Factorio Panel</h2>{% with m = get_flashed_messages() %}{% if m %}<p class=f>{{m[0]}}</p>{% endif %}{% endwith %}
<input type=text name=username placeholder="Username" autofocus style="margin-bottom:.5rem"><br>
<input type=password name=password placeholder="Password"><button>Enter</button></form></body></html>"""

PANEL_HTML = r"""<!doctype html><html><head><title>Factorio Panel</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{background:#1a1a24;color:#e8e8f0;font-family:system-ui;margin:0;padding:1rem;max-width:1000px;margin:auto}
.card{background:#242433;border-radius:12px;padding:1rem 1.2rem;margin-bottom:1rem}
h1{font-size:1.25rem}h3{margin:.2rem 0 .6rem;color:#e8902a}
button{padding:.45rem .9rem;border-radius:8px;border:0;color:#fff;font-weight:600;cursor:pointer;margin:.15rem .3rem .15rem 0;font-size:.85rem}
.g{background:#2e7d32}.r{background:#b3402a}.o{background:#e8902a}.b{background:#39508f}.gr{background:#3a3a4a}
#logs,#conout{background:#12121a;border-radius:8px;padding:.6rem;font-family:monospace;font-size:.72rem;max-height:280px;overflow-y:auto;white-space:pre-wrap}
input[type=text],input[type=password]{padding:.5rem;border-radius:8px;border:1px solid #444;background:#1a1a24;color:#fff}
textarea{width:100%;min-height:340px;background:#12121a;color:#cde;border:1px solid #444;border-radius:8px;font-family:monospace;font-size:.75rem;padding:.6rem;box-sizing:border-box}
table{width:100%;border-collapse:collapse;font-size:.85rem}td,th{padding:.35rem .5rem;text-align:left;border-bottom:1px solid #333}
.pill{display:inline-block;padding:.15rem .6rem;border-radius:999px;font-size:.8rem;font-weight:600}
.run{background:#2e7d3233;color:#7c6}.stop{background:#b3402a33;color:#e88}
.tabs{display:flex;gap:.3rem;margin-bottom:1rem;flex-wrap:wrap}
.tab{padding:.5rem 1rem;border-radius:8px;background:#242433;cursor:pointer;font-weight:600;font-size:.85rem}
.tab.active{background:#e8902a;color:#1a1a24}
.hidden{display:none}a{color:#8ab}.note{font-size:.75rem;color:#889}
.badge{font-size:.7rem;background:#39508f55;color:#9bd;border-radius:6px;padding:.1rem .4rem;margin-left:.3rem}
</style></head><body>
<h1>⚙️ Factorio Panel <span id=state class=pill></span> <span style="float:right;font-size:.8rem"><span id=whoami class=note></span> · <a href=/logout>logout</a></span></h1>
<div class=tabs>
<div class="tab active" data-t=dash>Dashboard</div><div class=tab data-t=chat>Chat</div><div class=tab data-t=mods>Mods</div>
<div class=tab data-t=settings>Settings</div><div class=tab data-t=world>World</div>
<div class=tab data-t=players>Players</div>
<div class=tab data-t=saves>Saves</div><div class=tab data-t=console>Console</div>
<div class=tab data-t=admin id=admintab style="display:none">Admin</div></div>

<div id=t-dash>
<div class=card><h3>Server</h3><div id=info>loading…</div>
<div style="margin-top:.6rem"><button class=g onclick="ctl('start')">Start</button>
<button class=o onclick="ctl('restart')">Restart</button><button class=r onclick="ctl('stop')">Stop</button></div>
<div id=cd style="color:#e8902a;margin-top:.5rem"></div></div>
<div class=card><h3>Online <span id=pcount></span></h3><div id=players>—</div>
<div style="margin-top:.6rem"><input type=text id=msg placeholder="Broadcast a message" style="width:60%"><button class=b onclick=say()>Say</button></div></div>
<div class=card><h3>Version</h3><div id=ver>…</div>
<div style="margin-top:.5rem"><input type=text id=newver placeholder="e.g. 2.1.18" style="width:110px"><button class=o onclick=upd()>Update</button>
<span class=note>backs up newest save, pins compose tag, pulls + recreates</span></div></div>
<div class=card><h3>Performance</h3><div id=perf>…</div></div>
<div class=card><h3>Logs</h3><div id=logs>…</div></div>
</div>

<div id=t-chat class=hidden>
<div class=card><h3>Game chat & events</h3>
<div id=chatlog style="background:#12121a;border-radius:8px;padding:.6rem;font-family:monospace;font-size:.78rem;max-height:420px;overflow-y:auto"></div>
<div style="margin-top:.6rem"><input type=text id=chatmsg placeholder="Send to game chat" style="width:70%" onkeydown="if(event.key==='Enter')chatSend()"><button class=b onclick=chatSend()>Send</button></div></div>
</div>

<div id=t-mods class=hidden>
<div class=card><h3>Installed mods</h3><div id=modlist>…</div>
<div style="margin-top:.6rem"><button class=b onclick=checkUpdates()>Check for updates</button><span id=updres class=note></span></div>
<p class=note>Enable/disable and installs apply after a server restart.</p></div>
<div class=card><h3>Install from mod portal</h3>
<input type=text id=modq placeholder="Search mods…" style="width:60%"><button class=b onclick=modSearch()>Search</button>
<div id=modresults style="margin-top:.6rem"></div></div>
<div class=card><h3>factorio.com credentials <span id=credstate class=badge></span></h3>
<p class=note>Needed for mod downloads. Username + token (from factorio.com → profile), or username + password (token fetched once, password not stored).</p>
<input type=text id=cuser placeholder="username" style="width:160px">
<input type=password id=ctoken placeholder="token (preferred)" style="width:220px">
<input type=password id=cpass placeholder="or password" style="width:160px">
<button class=o onclick=saveCreds()>Save</button></div>
</div>

<div id=t-settings class=hidden>
<div class=card><div style="display:flex;justify-content:space-between;align-items:center">
<h3 style="margin:0">Server settings</h3>
<label class=note style="cursor:pointer"><input type=checkbox id=advmode onchange=toggleAdv()> Advanced (raw JSON)</label></div>
<div id=settingsform style="margin-top:.8rem">loading…</div>
<div id=settingsadv class=hidden style="margin-top:.8rem">
<textarea id=settingsbox spellcheck=false></textarea>
<div style="margin-top:.5rem"><button class=o onclick=saveSettings()>Save raw JSON</button>
<button class=gr onclick=loadSettings()>Reload</button></div></div>
<div style="margin-top:.7rem"><button class=o id=formsave onclick=saveForm()>Save changes</button>
<button class=gr onclick=loadForm()>Reload</button>
<span class=note>a .bak is kept; restart the server to apply</span>
<div id=formerrors style="color:#f66;font-size:.8rem;margin-top:.4rem"></div></div></div>
</div>

<div id=t-world class=hidden>
<div class=card><h3>Current world behavior <span class=note>(map-settings — applies to the running map after restart)</span></h3>
<div id=worldform>loading…</div>
<div style="margin-top:.7rem"><button class=o onclick=saveWorld()>Save world settings</button>
<button class=gr onclick=loadWorld()>Reload</button>
<div id=worlderrors style="color:#f66;font-size:.8rem;margin-top:.4rem"></div></div></div>
<div class=card><h3>New map generation <span class=note>(map-gen — used only when creating a NEW map)</span></h3>
<div id=mapgenform>loading…</div>
<div style="margin-top:.7rem"><button class=o onclick=saveMapgen()>Save map-gen settings</button>
<button class=gr onclick=loadMapgen()>Reload</button>
<div id=mapgenerrors style="color:#f66;font-size:.8rem;margin-top:.4rem"></div></div>
<div style="margin-top:1rem;border-top:1px solid #333;padding-top:.8rem">
<input type=text id=newmapname placeholder="new map name" style="width:200px">
<button class=r onclick=newMap()>⚠ Generate new map & switch</button>
<span class=note>stops server, creates the map with the settings above, restarts into it (old saves kept)</span></div></div>
</div>

<div id=t-players class=hidden>
<div class=card><h3>Live players <span id=lpcount></span></h3>
<div style="display:flex;gap:1rem;flex-wrap:wrap;align-items:flex-start">
<canvas id=lpmap width=340 height=340 style="background:#12121a;border-radius:8px;flex-shrink:0"></canvas>
<div style="flex:1;min-width:260px"><div id=lptable>—</div></div></div>
<p class=note>Positions via RCON, refreshed every 5s while this tab is open. Map is centered on spawn (0,0); grid = 100 tiles.</p></div>
<div class=card><h3>Player actions</h3>
<input type=text id=pname placeholder="player name" style="width:160px">
<input type=text id=preason placeholder="reason (kick/ban)" style="width:200px"><br>
<button class=o onclick="pact('kick')">Kick</button><button class=r onclick="pact('ban')">Ban</button>
<button class=g onclick="pact('unban')">Unban</button><button class=b onclick="pact('promote')">Promote admin</button>
<button class=gr onclick="pact('demote')">Demote</button><button class=gr onclick="pact('mute')">Mute</button>
<button class=gr onclick="pact('unmute')">Unmute</button>
<div id=pactout class=note style="margin-top:.4rem"></div></div>
<div class=card><h3>Admin list</h3><div id=adminlist>…</div>
<input type=text id=newadmin placeholder="add player" style="width:160px"><button class=b onclick="listAdd('adminlist','newadmin')">Add</button></div>
<div class=card><h3>Ban list</h3><div id=banlist>…</div></div>
<div class=card><h3>Whitelist</h3><div id=whitelist>…</div>
<input type=text id=newwhite placeholder="add player" style="width:160px"><button class=b onclick="listAdd('whitelist','newwhite')">Add</button>
<p class=note>Whitelist only enforced if enabled in settings / with --use-server-whitelist.</p></div>
</div>

<div id=t-saves class=hidden>
<div class=card><h3>Boot save</h3><div id=bootsave>…</div>
<p class=note>Which save the server loads on start. "Latest" = newest file (autosaves win). Pinning a save recreates the container.</p></div>
<div class=card><h3>Saves</h3><button class=b onclick=backup()>Backup newest now</button>
<label class=gr style="padding:.45rem .9rem;border-radius:8px;font-weight:600;cursor:pointer;font-size:.85rem">Upload save<input type=file id=upfile accept=".zip" style="display:none" onchange=uploadSave()></label>
<div id=saves style="margin-top:.5rem">…</div></div>
</div>

<div id=t-console class=hidden>
<div class=card><h3>RCON console</h3>
<input type=text id=concmd placeholder="/players online  ·  /time  ·  /silent-command …" style="width:75%" onkeydown="if(event.key==='Enter')runCmd()">
<button class=o onclick=runCmd()>Run</button>
<div id=conout style="margin-top:.6rem">—</div>
<p class=note>⚠ /silent-command executes Lua with full game access and disables achievements. All commands audited.</p></div>
</div>

<div id=t-admin class=hidden>
<div class=card><h3>Panel users</h3><div id=userlist>…</div>
<div style="margin-top:.6rem">
<input type=text id=nu_name placeholder="username" style="width:130px">
<input type=password id=nu_pass placeholder="password (8+ chars)" style="width:170px">
<select id=nu_role style="padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">
<option>viewer</option><option>moderator</option><option selected>admin</option></select>
<button class=g onclick=userAdd()>Add user</button></div>
<p class=note>viewer: read-only · moderator: server control, players, saves, console · admin: everything</p></div>
<div class=card><h3>Change my password</h3>
<input type=password id=mypw placeholder="new password (8+ chars)" style="width:200px"><button class=o onclick=myPw()>Change</button></div>
<div class=card><h3>Discord notifications <span id=whstate class=badge></span></h3>
<p class=note>Webhook posts joins/leaves/chat to a Discord channel. Create one: channel settings → Integrations → Webhooks.</p>
<input type=password id=whurl placeholder="https://discord.com/api/webhooks/…" style="width:60%">
<label class=note><input type=checkbox id=ev_join checked> joins</label>
<label class=note><input type=checkbox id=ev_leave checked> leaves</label>
<label class=note><input type=checkbox id=ev_chat checked> chat</label>
<button class=o onclick=saveWebhook()>Save</button></div>
<div class=card><h3>Off-box backup</h3>
<button class=b onclick=offboxBackup()>Push newest save off-box now</button>
<span class=note>destination = backup_host in panel_config.json (SSH key auth required)</span>
<div id=offboxout class=note style="margin-top:.4rem"></div></div>
</div>

<script>
const j=(u,o)=>fetch(u,o).then(r=>r.json());
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
let MYROLE='viewer';
async function whoami(){const r=await j('/api/me');MYROLE=r.role;
 document.getElementById('whoami').textContent=r.username+' ('+r.role+')';
 if(r.role==='admin')document.getElementById('admintab').style.display='';}
whoami();
// chat
let chatSeen=0;
async function chatPoll(){const r=await j('/api/chat?since='+chatSeen);if(!r.ok)return;
 if(r.total<chatSeen){chatSeen=0;return chatPoll();}
 if(r.entries.length){const el=document.getElementById('chatlog');
  for(const e of r.entries){const c={'chat':'#cde','join':'#7c6','leave':'#e88'}[e.kind]||'#889';
   el.innerHTML+=`<div style="color:${c}">[${e.ts}] ${e.text.replace(/</g,'&lt;')}</div>`;}
  chatSeen=r.total;el.scrollTop=el.scrollHeight;}}
async function chatSend(){const m=document.getElementById('chatmsg').value.trim();if(!m)return;
 await post('/api/say',{message:m});document.getElementById('chatmsg').value='';}
setInterval(chatPoll,4000);chatPoll();
// perf
async function perf(){const r=await j('/api/metrics');if(!r.ok)return;
 const h=r.history;const last=h[h.length-1]||{};
 document.getElementById('perf').textContent=
 `UPS ${r.ups??'—'}/60 · CPU ${last.cpu??'—'}% · RAM ${last.mem??'—'} · players ${last.players??0}`;}
setInterval(perf,30000);perf();
// admin
async function usersUI(){const r=await j('/api/users');if(!r.ok)return;
 document.getElementById('userlist').innerHTML='<table><tr><th>user</th><th>role</th><th></th></tr>'+
 r.users.map(u=>`<tr><td>${u.username}</td><td>
 <select onchange="userRole('${u.username}',this.value)" style="padding:.3rem;border-radius:6px;background:#1a1a24;color:#fff;border:1px solid #444">
 ${['viewer','moderator','admin'].map(x=>`<option ${x===u.role?'selected':''}>${x}</option>`).join('')}</select></td>
 <td><a href=# onclick="userDel('${u.username}');return false" style="color:#e88">✕</a></td></tr>`).join('')+'</table>';}
async function userAdd(){const r=await post('/api/users',{action:'add',username:nu_name.value.trim(),password:nu_pass.value,role:nu_role.value});
 if(!r.ok)alert(r.error);nu_pass.value='';usersUI();}
async function userDel(u){if(!confirm('Delete user '+u+'?'))return;const r=await post('/api/users',{action:'delete',username:u});if(!r.ok)alert(r.error);usersUI();}
async function userRole(u,role){const r=await post('/api/users',{action:'setrole',username:u,role});if(!r.ok){alert(r.error);usersUI();}}
async function myPw(){const r=await post('/api/users',{action:'setpassword',password:mypw.value});alert(r.ok?'Changed.':r.error);mypw.value='';}
async function webhookUI(){const r=await j('/api/panelconfig');if(!r.ok)return;
 document.getElementById('whstate').textContent=r.discord_webhook_set?'set ✓':'not set';
 for(const e of['join','leave','chat'])document.getElementById('ev_'+e).checked=r.discord_events.includes(e);}
async function saveWebhook(){const evs=['join','leave','chat'].filter(e=>document.getElementById('ev_'+e).checked);
 const b={discord_events:evs};const u=whurl.value.trim();if(u)b.discord_webhook=u;
 const r=await post('/api/panelconfig',b);alert(r.ok?'Saved.':r.error);whurl.value='';webhookUI();}
async function offboxBackup(){document.getElementById('offboxout').textContent='pushing…';
 const r=await post('/api/backup-offbox',{});
 document.getElementById('offboxout').textContent=r.ok?'✓ '+r.dest:'Failed: '+r.error;}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
 document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));t.classList.add('active');
 ['dash','chat','mods','settings','world','players','saves','console','admin'].forEach(n=>document.getElementById('t-'+n).classList.toggle('hidden',n!==t.dataset.t));
 if(t.dataset.t==='mods')mods();if(t.dataset.t==='settings')loadForm();
 if(t.dataset.t==='world'){loadWorld();loadMapgen();}
 if(t.dataset.t==='admin'){usersUI();webhookUI();}
 if(t.dataset.t==='players'){lists();lpStart();}if(t.dataset.t==='saves'){saves();bootsaveUI();}});
async function refresh(){const s=await j('/api/status');
 const st=document.getElementById('state');st.textContent=s.container.state;
 st.className='pill '+(s.container.state==='running'?'run':'stop');
 document.getElementById('info').textContent=`image ${s.container.image||'—'} · up ${s.container.uptime||'—'} · v${s.version||'—'}`;
 document.getElementById('players').textContent=s.players.length?s.players.join(', '):'nobody online';
 document.getElementById('pcount').textContent=`(${s.players.length})`;
 document.getElementById('cd').textContent=s.countdown.countdown?`⏳ ${s.countdown.action} in ${s.countdown.seconds_left}s (players warned)`:'';}
async function logs(){const l=await j('/api/logs');const e=document.getElementById('logs');e.textContent=l.logs.join('\n');e.scrollTop=e.scrollHeight;}
async function ver(){const v=await j('/api/versions');document.getElementById('ver').textContent=`running ${v.running} · stable ${v.stable||'?'} · experimental ${v.experimental||'?'}`;}
async function ctl(a){if(a!=='start'&&!confirm(a+' the server?'))return;await post('/api/control',{action:a});setTimeout(refresh,1500);}
async function say(){const m=document.getElementById('msg').value;if(!m)return;await post('/api/say',{message:m});document.getElementById('msg').value='';}
async function upd(){const v=document.getElementById('newver').value.trim();if(!v||!confirm('Update to '+v+'?'))return;
 const r=await post('/api/update',{version:v});alert(r.ok?'Updated, server restarting.':'Failed: '+(r.error||r.output));ver();refresh();}
// mods
async function mods(){const d=await j('/api/mods');
 document.getElementById('credstate').textContent=d.creds_set?'set ✓':'not set';
 document.getElementById('modlist').innerHTML='<table><tr><th>mod</th><th>version</th><th>enabled</th><th></th></tr>'+
 d.mods.map(m=>`<tr><td>${m.name}${m.builtin?' <span class=badge>builtin</span>':''}${m.unlisted?' <span class=badge>unlisted</span>':''}</td>
 <td>${m.version||'—'}</td><td>${m.enabled?'✅':'—'}</td><td>
 <button class=gr onclick="modToggle('${m.name}')">${m.enabled?'disable':'enable'}</button>
 ${!m.builtin?`<button class=r onclick="modRemove('${m.name}')">remove</button><button class=b onclick="modUpdate('${m.name}')">update</button>`:''}
 </td></tr>`).join('')+'</table>';}
async function modToggle(n){await post('/api/mods/toggle',{name:n});mods();}
async function modRemove(n){if(!confirm('Remove mod '+n+'?'))return;const r=await post('/api/mods/remove',{name:n});if(!r.ok)alert(r.error);mods();}
async function modUpdate(n){const r=await post('/api/mods/update',{name:n});alert(r.ok?n+' → '+r.version:'Failed: '+r.error);mods();}
async function checkUpdates(){document.getElementById('updres').textContent='checking…';
 const r=await j('/api/mods/check-updates');
 document.getElementById('updres').textContent=r.updates.length?r.updates.map(u=>`${u.name} ${u.installed}→${u.available}`).join(' · '):'all current';}
async function modSearch(){const q=document.getElementById('modq').value.trim();if(!q)return;
 const r=await j('/api/mods/search?q='+encodeURIComponent(q));
 if(!r.ok){alert(r.error);return;}
 document.getElementById('modresults').innerHTML='<table><tr><th>mod</th><th>by</th><th>DLs</th><th>latest</th><th></th></tr>'+
 r.results.map(m=>`<tr><td title="${m.summary}">${m.title}<br><span class=note>${m.name}</span></td><td>${m.owner}</td>
 <td>${m.downloads}</td><td>${m.latest||'?'}</td><td><button class=g onclick="modInstall('${m.name}')">install</button></td></tr>`).join('')+'</table>';}
async function modInstall(n){const r=await post('/api/mods/install',{name:n});
 alert(r.ok?'Installed: '+r.installed.map(x=>x.name+' '+x.version).join(', ')+'. Restart to apply.':'Failed: '+r.error);mods();}
async function saveCreds(){const r=await post('/api/credentials',{username:cuser.value.trim(),token:ctoken.value.trim(),password:cpass.value});
 alert(r.ok?'Credentials saved.':'Failed: '+r.error);cpass.value='';mods();}
// world tab — shared schema-form renderer
function renderSchemaForm(el,sections,prefix){document.getElementById(el).innerHTML=sections.map(sec=>
 `<h3 style="margin-top:1rem;font-size:.95rem">${sec.section}</h3><table>`+sec.fields.map(f=>{
  const id=prefix+f.key.replace(/[.\-]/g,'__');let ctl='';
  const st='padding:.35rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444';
  if(f.type==='bool')ctl=`<input type=checkbox id=${id} ${f.value?'checked':''}>`;
  else if(f.type==='int'||f.type==='float')ctl=`<input type=number step=any id=${id} value="${f.value??''}" style="width:130px;${st}">`;
  else if(f.type==='int_or_null')ctl=`<input type=text id=${id} value="${f.value??''}" placeholder=random style="width:130px;${st}">`;
  else ctl=`<input type=text id=${id} value="${String(f.value??'')}" style="width:200px;${st}">`;
  return `<tr><td style="width:52%">${f.label}${f.help?`<br><span class=note>${f.help}</span>`:''}</td><td>${ctl}</td></tr>`;
 }).join('')+'</table>').join('');}
function collectSchemaForm(sections,prefix){const values={};
 for(const sec of sections)for(const f of sec.fields){
  const el=document.getElementById(prefix+f.key.replace(/[.\-]/g,'__'));if(!el)continue;
  values[f.key]=f.type==='bool'?el.checked:el.value;}
 return values;}
let worldSchema=null,mapgenSchema=null;
async function loadWorld(){const r=await j('/api/world');worldSchema=r.sections;renderSchemaForm('worldform',r.sections,'wf_');}
async function saveWorld(){const r=await post('/api/world',{values:collectSchemaForm(worldSchema,'wf_')});
 document.getElementById('worlderrors').textContent=r.ok?'':'Validation: '+(r.errors||[r.error]).join(' · ');
 if(r.ok)alert('Saved. '+r.note);}
async function loadMapgen(){const r=await j('/api/mapgen');mapgenSchema=r.sections;renderSchemaForm('mapgenform',r.sections,'mg_');}
async function saveMapgen(){const r=await post('/api/mapgen',{values:collectSchemaForm(mapgenSchema,'mg_')});
 document.getElementById('mapgenerrors').textContent=r.ok?'':'Validation: '+(r.errors||[r.error]).join(' · ');
 if(r.ok)alert('Saved. '+r.note);}
async function newMap(){const n=document.getElementById('newmapname').value.trim();if(!n)return;
 if(!confirm('Generate NEW map "'+n+'" and switch the server to it? Current world stays on disk but the server will boot the new one.'))return;
 const r=await post('/api/newmap',{name:n});alert(r.ok?'Generating '+r.file+' — server restarting into it (~30s).':'Failed: '+r.error);}
async function bootsaveUI(){const r=await j('/api/bootsave');const d=await j('/api/saves');
 document.getElementById('bootsave').innerHTML=
 `<select id=bootsel style="padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">
 <option value="">Latest save (default)</option>`+
 d.saves.map(s=>`<option value="${s.name}" ${r.save&&s.name.startsWith(r.save)?'selected':''}>${s.name}</option>`).join('')+
 `</select> <button class=o onclick=setBootsave()>Apply</button>
 <span class=note>current: ${r.mode==='latest'?'latest':r.save}</span>`;}
async function setBootsave(){const v=document.getElementById('bootsel').value;
 if(!confirm(v?'Pin boot save to '+v+'? (container recreates now)':'Boot latest save? (container recreates now)'))return;
 const r=await post('/api/bootsave',{save:v});alert(r.ok?'Applied.':'Failed: '+r.error);bootsaveUI();}
// settings — forms mode
let formSchema=null;
async function loadForm(){const r=await j('/api/settings-form');formSchema=r.sections;
 document.getElementById('settingsform').innerHTML=r.sections.map(sec=>
 `<h3 style="margin-top:1rem">${sec.section}</h3><table>`+sec.fields.map(f=>{
  const id='sf_'+f.key.replace(/\./g,'__');let ctl='';
  if(f.type==='bool')ctl=`<input type=checkbox id=${id} ${f.value?'checked':''}>`;
  else if(f.type==='enum')ctl=`<select id=${id} style="padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">`+
    f.choices.map(c=>`<option ${c===f.value?'selected':''}>${c}</option>`).join('')+'</select>';
  else if(f.type==='int')ctl=`<input type=number id=${id} value="${f.value??''}" min="${f.min??''}" max="${f.max??''}" style="width:110px;padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">`;
  else if(f.type==='secret')ctl=`<input type=password id=${id} value="${f.value==='__SET__'?'__SET__':''}" placeholder="${f.value==='__SET__'?'(set — type to change)':'(empty)'}" style="width:220px;padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">`;
  else ctl=`<input type=text id=${id} value="${String(f.value??'').replace(/"/g,'&quot;')}" style="width:min(340px,90%);padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">`;
  return `<tr><td style="width:45%">${f.label}${f.help?`<br><span class=note>${f.help}</span>`:''}</td><td>${ctl}</td></tr>`;
 }).join('')+'</table>').join('');}
async function saveForm(){if(!formSchema)return;const values={};
 for(const sec of formSchema)for(const f of sec.fields){
  const el=document.getElementById('sf_'+f.key.replace(/\./g,'__'));if(!el)continue;
  values[f.key]=f.type==='bool'?el.checked:el.value;}
 const r=await post('/api/settings-form',{values});
 document.getElementById('formerrors').textContent=r.ok?'':'Validation: '+(r.errors||[r.error]).join(' · ');
 if(r.ok)alert('Saved. Restart the server to apply.');}
function toggleAdv(){const adv=document.getElementById('advmode').checked;
 document.getElementById('settingsadv').classList.toggle('hidden',!adv);
 document.getElementById('settingsform').classList.toggle('hidden',adv);
 document.getElementById('formsave').style.display=adv?'none':'';
 if(adv)loadSettings();else loadForm();}
async function loadSettings(){const r=await j('/api/settings');document.getElementById('settingsbox').value=JSON.stringify(r.settings,null,2);}
async function saveSettings(){let v;try{v=JSON.parse(document.getElementById('settingsbox').value)}catch(e){alert('Invalid JSON: '+e);return;}
 const r=await post('/api/settings',{settings:v});alert(r.ok?'Saved. Restart to apply.':'Failed: '+r.error);}
// players
// live players
let lpTimer=null;
const LP_COLORS=['#e8902a','#6c9','#8ab','#e88','#c9d','#dd7'];
async function livePlayers(){const r=await j('/api/liveplayers');if(!r.ok)return;
 const ps=r.players;document.getElementById('lpcount').textContent='('+ps.length+')';
 document.getElementById('lptable').innerHTML=ps.length?
 '<table><tr><th></th><th>player</th><th>pos</th><th>surface</th><th>online</th><th>afk</th></tr>'+
 ps.map((p,i)=>`<tr><td><span style="color:${LP_COLORS[i%6]}">●</span></td><td>${p.name}${p.admin?' <span class=badge>admin</span>':''}</td>
 <td>${p.x}, ${p.y}</td><td>${p.surface}</td><td>${fmtMin(p.online)}</td><td>${p.afk>1?fmtMin(p.afk):'—'}</td></tr>`).join('')+'</table>'
 :'<span class=note>nobody online</span>';
 drawMap(ps);}
function fmtMin(m){return m>=60?Math.floor(m/60)+'h '+(m%60)+'m':m+'m';}
function drawMap(ps){const cv=document.getElementById('lpmap'),ctx=cv.getContext('2d');
 ctx.clearRect(0,0,340,340);
 // scale: fit all players + margin, min ±200 tiles
 let ext=200;for(const p of ps)ext=Math.max(ext,Math.abs(p.x)*1.2,Math.abs(p.y)*1.2);
 const sc=160/ext;
 ctx.strokeStyle='#1e1e2c';ctx.lineWidth=1;
 const step=100*sc;
 for(let g=170%step;g<340;g+=step){ctx.beginPath();ctx.moveTo(g,0);ctx.lineTo(g,340);ctx.stroke();
  ctx.beginPath();ctx.moveTo(0,g);ctx.lineTo(340,g);ctx.stroke();}
 ctx.strokeStyle='#333';ctx.beginPath();ctx.moveTo(170,0);ctx.lineTo(170,340);ctx.stroke();
 ctx.beginPath();ctx.moveTo(0,170);ctx.lineTo(340,170);ctx.stroke();
 ctx.fillStyle='#556';ctx.font='9px monospace';ctx.fillText('(0,0)',173,167);
 ps.forEach((p,i)=>{const x=170+p.x*sc,y=170+p.y*sc;
  ctx.fillStyle=LP_COLORS[i%6];ctx.beginPath();ctx.arc(x,y,5,0,7);ctx.fill();
  ctx.fillStyle='#cde';ctx.font='10px system-ui';ctx.fillText(p.name,x+7,y+3);});}
function lpStart(){livePlayers();if(!lpTimer)lpTimer=setInterval(()=>{
 if(document.getElementById('t-players').classList.contains('hidden')){clearInterval(lpTimer);lpTimer=null;return;}
 livePlayers();},5000);}
async function pact(a){const p=document.getElementById('pname').value.trim();if(!p)return;
 const r=await post('/api/player-action',{action:a,player:p,reason:document.getElementById('preason').value.trim()});
 document.getElementById('pactout').textContent=r.ok?(r.output||'ok'):'Failed: '+r.error;lists();}
async function lists(){for(const k of['adminlist','banlist','whitelist']){const r=await j('/api/list/'+k);
 const el=document.getElementById(k);
 if(!r.entries.length){el.innerHTML='<span class=note>empty</span>';continue;}
 el.innerHTML=r.entries.map((e,i)=>{const n=typeof e==='string'?e:(e.username||JSON.stringify(e));
 return `<span style="display:inline-block;background:#1a1a24;border-radius:8px;padding:.2rem .6rem;margin:.15rem">${n}
 <a href=# onclick="listDel('${k}',${i});return false" style="color:#e88">✕</a></span>`}).join('');}}
async function listDel(k,i){const r=await j('/api/list/'+k);r.entries.splice(i,1);
 await post('/api/list/'+k,{entries:r.entries});lists();}
async function listAdd(k,inp){const v=document.getElementById(inp).value.trim();if(!v)return;
 const r=await j('/api/list/'+k);r.entries.push(v);await post('/api/list/'+k,{entries:r.entries});
 document.getElementById(inp).value='';lists();}
// saves
async function saves(){const d=await j('/api/saves');document.getElementById('saves').innerHTML=
 '<table><tr><th>save</th><th>size</th><th>modified</th><th></th></tr>'+d.saves.map(s=>
 `<tr><td>${s.name}</td><td>${s.size_mb} MB</td><td>${s.mtime}</td>
 <td><a href="/saves/download/${s.name}">⬇</a> <a href=# onclick="delSave('${s.name}');return false" style="color:#e88">✕</a></td></tr>`).join('')+'</table>';}
async function delSave(n){if(!confirm('Delete '+n+'?'))return;const r=await post('/api/saves/delete',{name:n});if(!r.ok)alert(r.error);saves();}
async function backup(){const r=await post('/api/saves/backup',{});alert(r.ok?'Backed up: '+r.name:'Failed');saves();}
async function uploadSave(){const f=document.getElementById('upfile').files[0];if(!f)return;
 const fd=new FormData();fd.append('file',f);
 const r=await fetch('/api/saves/upload',{method:'POST',body:fd}).then(x=>x.json());
 alert(r.ok?'Uploaded '+r.name:'Failed: '+r.error);saves();}
// console
async function runCmd(){const c=document.getElementById('concmd').value.trim();if(!c)return;
 const r=await post('/api/rcon',{command:c});const o=document.getElementById('conout');
 o.textContent+=`\n> ${c}\n${r.ok?(r.output||'(no output)'):'ERROR: '+r.error}`;o.scrollTop=o.scrollHeight;
 document.getElementById('concmd').value='';}
refresh();logs();ver();setInterval(refresh,5000);setInterval(()=>{if(!document.getElementById('t-dash').classList.contains('hidden'))logs()},10000);
</script></body></html>"""

if __name__ == "__main__":
    app.run(host=os.environ.get("PANEL_BIND", "127.0.0.1"), port=int(os.environ.get("PANEL_PORT", "8920")))

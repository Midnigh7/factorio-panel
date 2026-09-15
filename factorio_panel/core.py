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

# ── rate limiting (in-memory, per-IP sliding windows) ────────────────────────
_rl_lock = threading.Lock()
_rl_buckets = {}   # key -> [timestamps]

def _rl_check(key, limit, window_s):
    """True if the call is allowed; records the hit."""
    now = time.time()
    with _rl_lock:
        hits = _rl_buckets.setdefault(key, [])
        hits[:] = [t for t in hits if now - t < window_s]
        if len(hits) >= limit:
            return False
        hits.append(now)
        if len(_rl_buckets) > 5000:  # bound memory against IP churn
            for k in [k for k, v in _rl_buckets.items() if not v][:1000]:
                _rl_buckets.pop(k, None)
        return True

def rate_limit(limit, window_s, scope="ip"):
    """Decorator: cap calls per remote IP (or ip+path) inside a sliding window."""
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            key = f"{request.remote_addr}:{f.__name__}" if scope == "ip" else request.remote_addr
            if not _rl_check(key, limit, window_s):
                audit(f"RATELIMIT {f.__name__}")
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "rate limited — slow down"}), 429
                return "Too many requests — try again shortly.", 429
            return f(*a, **k)
        return w
    return deco

# login lockout: consecutive failures earn a cooldown that survives page reloads
_fail_lock = threading.Lock()
_login_fails = {}  # ip -> [timestamps]

def login_failed(ip):
    with _fail_lock:
        fails = _login_fails.setdefault(ip, [])
        now = time.time()
        fails[:] = [t for t in fails if now - t < 900]
        fails.append(now)

def login_blocked(ip):
    with _fail_lock:
        fails = _login_fails.get(ip, [])
        now = time.time()
        fails[:] = [t for t in fails if now - t < 900]
        return len(fails) >= 8   # 8 failures in 15 min → locked out for the window

def login_succeeded(ip):
    with _fail_lock:
        _login_fails.pop(ip, None)

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

from .core import *  # noqa: F401,F403 — shared app, helpers, flask names

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
@rate_limit(4, 3600)
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

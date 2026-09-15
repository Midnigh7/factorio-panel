from .core import *  # noqa: F401,F403 — shared app, helpers, flask names

from .ui import LOGIN_HTML, PANEL_HTML

# ── routes: auth/basic ───────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
@rate_limit(30, 60)          # page loads + attempts: 30/min/IP
def login():
    if request.method == "POST":
        ip = request.remote_addr
        if login_blocked(ip):
            audit(f"LOGIN blocked (lockout) ip={ip}")
            flash("Too many failed attempts. Locked out for 15 minutes.")
            return render_template_string(LOGIN_HTML), 429
        time.sleep(0.5)
        username = (request.form.get("username") or "").strip()
        pw = request.form.get("password", "")
        u = find_user(username) if username else None
        if u and verify_pw(pw, u["pw"]):
            login_succeeded(ip)
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
        login_failed(ip)
        audit(f"LOGIN failed user={username!r}")
        flash("Wrong username or password.")
    return render_template_string(LOGIN_HTML)

@app.route("/api/me")
@login_required
def api_me():
    return jsonify({"ok": True, "username": session.get("username"), "role": current_role()})

@app.route("/api/users", methods=["GET", "POST"])
@rate_limit(30, 60)
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

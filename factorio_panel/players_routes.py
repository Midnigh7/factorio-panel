from .core import *  # noqa: F401,F403 — shared app, helpers, flask names

# ── routes: console & player admin ───────────────────────────────────────────
@app.route("/api/rcon", methods=["POST"])
@rate_limit(30, 60)
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
@rate_limit(20, 60)
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

from .core import *  # noqa: F401,F403 — shared app, helpers, flask names

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

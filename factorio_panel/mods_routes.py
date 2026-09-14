from .core import *  # noqa: F401,F403 — shared app, helpers, flask names

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

from .core import *  # noqa: F401,F403 — shared app, helpers, flask names

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

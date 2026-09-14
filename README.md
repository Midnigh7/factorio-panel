# Factorio Panel

A modern, single-file web UI for managing a **dockerized Factorio server**. Built as a spiritual successor to [factorio-server-manager](https://github.com/OpenFactorioServerManager/factorio-server-manager) (last released 2020) — but Docker-native, so your server updates are just an image tag change.

![panel](docs/screenshot.png)

## Features

- **Dashboard** — container status, uptime, version, start/stop/restart with in-game countdown warnings when players are online, broadcast, live logs, UPS/CPU/RAM metrics
- **Chat** — live game chat & join/leave feed in the browser, send messages to the game, optional **Discord webhook bridge** (joins, leaves, chat)
- **Mods** — full mod-portal search (client-side index of all ~23k mods, sorted by downloads), one-click install **with recursive dependency resolution**, game-version-aware release picking, update checker, per-mod update, enable/disable, remove
- **Settings** — `server-settings.json` as validated form sections (General / Visibility / Gameplay / Autosave / Network) with an Advanced raw-JSON mode; secrets masked; `.bak` before every write
- **World** — `map-settings.json` editor (evolution, expansion, pollution, difficulty), `map-gen-settings.json` editor (resources, terrain, enemies, seed), and a **"generate new map & switch"** flow
- **Players** — kick/ban/unban/promote/demote/mute via RCON, visual admin/ban/whitelist editors
- **Saves** — list/backup/upload/download/delete, **boot-save pinning** (choose which save the server loads), off-box backup push over SSH
- **Console** — raw RCON with scrollback
- **Users & roles** — viewer / moderator / admin with per-user audit logging (PBKDF2, no external deps)
- **Version management** — shows running vs latest stable/experimental, one-field update that backs up saves first and pins an exact image tag (no `latest` roulette)

Single Python file. Flask is the only dependency.

## Requirements

- A Factorio server running in Docker via [factoriotools/factorio](https://hub.docker.com/r/factoriotools/factorio) with a compose file
- RCON exposed to the panel host (the factoriotools image writes `config/rconpw`)
- Python 3.10+, the panel user in the `docker` group

## Quick start

```bash
git clone https://github.com/Midnigh7/factorio-panel
cd factorio-panel
python3 -m venv venv && venv/bin/pip install flask
# generate an initial master password hash:
python3 -c "import hashlib,secrets; pw=secrets.token_urlsafe(12); print('password:',pw); print('PASSWORD_SHA256='+hashlib.sha256(pw.encode()).hexdigest())"
cat > panel.env <<EOF
PASSWORD_SHA256=<hash from above>
SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))")
EOF
chmod 600 panel.env
PANEL_BIND=0.0.0.0 venv/bin/python app.py
```

First login with **any username + the master password** creates that user as admin. Add real users in the Admin tab afterward; the master password stops working once users exist.

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PANEL_DIR` | `~/factorio-panel` | panel state (users, config, audit log) |
| `FACTORIO_COMPOSE` | `~/factorio/compose.yml` | compose file for the server |
| `FACTORIO_DATA` | `/opt/factorio` | host path of the factorio volume |
| `FACTORIO_CONTAINER` | `factorio` | container name |
| `FACTORIO_CONT_ROOT` | `/factorio` | volume mount point inside the container |
| `RCON_HOST` / `RCON_PORT` | `127.0.0.1` / `27015` | RCON endpoint |
| `PANEL_BIND` / `PANEL_PORT` | `127.0.0.1` / `8920` | where the panel listens |
| `BACKUP_HOST` / `BACKUP_DIR` | — / `~/factorio-backups` | off-box backup target (`user@host`, SSH key auth) |

### Example compose file for the game server

```yaml
services:
  factorio:
    container_name: factorio
    image: factoriotools/factorio:2.0.77   # pin an exact tag; the panel manages updates
    restart: unless-stopped
    ports:
      - "34197:34197/udp"
      - "127.0.0.1:27015:27015"
    volumes:
      - /opt/factorio:/factorio
    environment:
      - LOAD_LATEST_SAVE=true
```

### systemd unit

```ini
[Unit]
Description=Factorio management panel
After=network-online.target

[Service]
ExecStart=/home/you/factorio-panel/venv/bin/python /home/you/factorio-panel/app.py
WorkingDirectory=/home/you/factorio-panel
Environment=PANEL_BIND=0.0.0.0
Restart=always

[Install]
WantedBy=default.target
```

> If your user's docker group membership is newer than your login session, wrap ExecStart in `sg docker -c "..."`.

## Security notes

- Run it behind a reverse proxy with TLS (Caddy/nginx/Traefik); the panel itself speaks plain HTTP
- Designed for LAN/VPN use. If you must expose it, put real authentication (e.g. an identity-aware proxy) in front — the built-in auth is solid for a household, not for the open internet
- Mod downloads require your factorio.com username + token (Mods tab → Credentials); the token is stored `0600` on disk, never displayed again
- Every state-changing action is written to a rotating audit log with username + IP

## License

GPL-3.0

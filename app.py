#!/usr/bin/env python3
"""Factorio Panel — entry point.

Run directly (python app.py) or via gunicorn (app:app).
All configuration is via environment variables — see README.
"""
import os
from factorio_panel import app  # noqa: F401 — importing registers routes + watchers

if __name__ == "__main__":
    app.run(host=os.environ.get("PANEL_BIND", "127.0.0.1"),
            port=int(os.environ.get("PANEL_PORT", "8920")))

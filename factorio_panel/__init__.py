"""factorio_panel — modular Factorio server management panel.

Importing the submodules registers all routes on the shared Flask app
(created in core) and starts the chat/metrics watcher threads.
"""
from .core import app  # noqa: F401
from . import chatmetrics  # noqa: F401 — starts chat + metrics watcher threads
from . import auth_routes  # noqa: F401
from . import players_routes  # noqa: F401
from . import settings_routes  # noqa: F401
from . import saves_routes  # noqa: F401
from . import mods_routes  # noqa: F401

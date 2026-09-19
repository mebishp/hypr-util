"""The laptop keyboard's saved look.

One file, written by whoever is driving the UI and read by the root daemon
when it starts or when the machine resumes. The daemon never writes here:
the file lives in the desktop user's config directory, and a root-owned file
appearing in it is a file the user's own app can no longer replace.
"""
import json
import os
from pathlib import Path

from . import zones
from ..util import CONFIG_DIR, atomic_write_text


def kbd_dir():
    """Where the saved look lives.

    The daemon runs as root and so has a different HOME; setup.sh gives its
    unit the installing user's config directory in the environment, exactly
    as it does for the fan curve daemon.
    """
    override = os.environ.get("HYPR_UTIL_CONFIG_DIR")
    return (Path(override) if override else CONFIG_DIR) / "kbd"


def current_path():
    return kbd_dir() / "current.json"


def read_current():
    """The look to show, defaults included."""
    try:
        return zones.normalize_look(json.loads(current_path().read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return zones.normalize_look({})


def write_current(look):
    look = zones.normalize_look(look)
    directory = kbd_dir()
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / "current.json", json.dumps(look, indent=2))
    return look

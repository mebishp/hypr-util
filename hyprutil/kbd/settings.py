"""Everything about the laptop keyboard that is not the look itself.

The look -- colours, effect, brightness -- changes constantly and lives in
current.json, which the effects service watches. This is the other half:
which indicators are on, what the battery thresholds are, whether the two
keyboards follow each other. It changes rarely, so it is a separate file
that nothing polls in a hot loop.

Written by the settings app and the CLI, read by both of those and by the
effects service.
"""
import json
import os
from pathlib import Path

from . import zones
from ..util import CONFIG_DIR, atomic_write_text

# Zone defaults chosen so the two indicators do not land on top of each
# other, and so neither takes the middle of the board -- that is the part
# a person actually looks at while typing.
DEFAULTS = {
    "indicators": {
        "enabled": True,
        # Keep showing status on a keyboard whose lighting is switched off.
        # The backlight goes on with every other zone written black, which
        # reads as a dark keyboard with one status light -- measured on this
        # board: a zone set to 000000 is genuinely dark, not dimly lit.
        "when_off": True,
        "profile": {"enabled": True, "zone": zones.ZONE_RIGHT},
        "battery": {
            "enabled": True,
            "zone": zones.ZONE_LEFT,
            "low": 25,
            "critical": 10,
            "show_charging": True,
        },
    },
    "battery_saver": {
        # Lighting is a real draw on a laptop: four zones at full brightness
        # is not free. On battery the colours are scaled down and animations
        # can be stopped, without touching the look the user chose -- unplug
        # and replug and it comes back exactly as it was.
        "enabled": False,
        "brightness": 40,
        "static_only": True,
    },
    "sync": {
        # Make the external Firefly follow the laptop keyboard's colour, so
        # the two do not disagree about what colour the machine is.
        "enabled": False,
        "source_zone": zones.ZONE_LEFT,
        "match_effect": True,
    },
}


def settings_dir():
    override = os.environ.get("HYPR_UTIL_CONFIG_DIR")
    return (Path(override) if override else CONFIG_DIR) / "kbd"


def settings_path():
    return settings_dir() / "settings.json"


def _merge(defaults, data):
    """Defaults, with `data` laid over it one level at a time.

    A plain dict.update would let a half-written file drop whole sections;
    merging key by key means a file that only says {"sync": {"enabled":
    true}} still gets every other default.
    """
    out = {}
    for key, fallback in defaults.items():
        value = (data or {}).get(key)
        if isinstance(fallback, dict):
            out[key] = _merge(fallback, value if isinstance(value, dict) else {})
        elif isinstance(fallback, bool):
            out[key] = bool(value) if value is not None else fallback
        elif isinstance(fallback, int):
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                out[key] = fallback
        else:
            out[key] = value if value is not None else fallback
    return out


def normalize(data):
    out = _merge(DEFAULTS, data)
    battery = out["indicators"]["battery"]
    battery["low"] = max(1, min(100, battery["low"]))
    # A critical threshold at or above the low one would mean the low state
    # could never be reached; clamp rather than refuse, since this comes
    # from a file a person may have edited.
    battery["critical"] = max(1, min(battery["low"] - 1, battery["critical"])) \
        if battery["low"] > 1 else 1
    for section, key in (("indicators", "profile"), ("indicators", "battery")):
        zone = out[section][key]["zone"]
        if zone not in zones.ZONE_NAMES:
            zone = DEFAULTS[section][key]["zone"]
        out[section][key]["zone"] = zone
    saver = out["battery_saver"]
    saver["brightness"] = max(0, min(zones.BRIGHTNESS_MAX, saver["brightness"]))
    if out["sync"]["source_zone"] not in zones.ZONE_NAMES:
        out["sync"]["source_zone"] = DEFAULTS["sync"]["source_zone"]
    return out


def read():
    try:
        return normalize(json.loads(settings_path().read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return normalize({})


def write(data):
    data = normalize(data)
    directory = settings_dir()
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(settings_path(), json.dumps(data, indent=2))
    return data

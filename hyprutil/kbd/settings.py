"""Everything about the laptop keyboard that is not the look itself.

The look -- colours, effect, brightness -- changes constantly and lives in
current.json, which the effects service watches. This is the other half:
which indicators are on and what the battery thresholds are. It changes
rarely, so it is a separate file that nothing polls in a hot loop.

Written by the settings app and the CLI, read by both of those and by the
effects service.
"""
import json
import os
from pathlib import Path

from . import indicators, zones
from ..util import CONFIG_DIR, atomic_write_text

# Indicators have no zone any more: an alert is the whole keyboard for a
# couple of seconds and then the look comes back. See kbd/indicators.py for
# why that replaced a permanently reserved zone. A `zone` left in an old
# settings file is simply not read -- _merge only ever takes the keys the
# defaults below name.
DEFAULTS = {
    "indicators": {
        "enabled": True,
        # Keep showing status on a keyboard whose lighting is switched off:
        # the backlight comes on for the alert and goes back off after it.
        "when_off": True,
        # How long the board is held. Long enough to catch out of the corner
        # of your eye, short enough not to feel like the app has hung.
        "duration": indicators.DEFAULT_DURATION,
        "profile": {"enabled": True},
        "battery": {
            "enabled": True,
            "low": 25,
            "critical": 10,
            "show_charging": True,
            # A critical battery is the one thing worth saying twice.
            "repeat_critical": indicators.DEFAULT_CRITICAL_REPEAT,
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
}


def settings_dir():
    override = os.environ.get("HYPR_UTIL_CONFIG_DIR")
    return (Path(override) if override else CONFIG_DIR) / "kbd"


def settings_path():
    return settings_dir() / "settings.json"


def _merge(defaults, data):
    """Defaults, with `data` laid over it one level at a time.

    A plain dict.update would let a half-written file drop whole sections;
    merging key by key means a file that only says {"battery_saver":
    {"enabled": true}} still gets every other default.
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


def _number(value, fallback, low, high):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


def normalize(data):
    out = _merge(DEFAULTS, data)
    ind = out["indicators"]
    ind["duration"] = _number(
        ind["duration"], indicators.DEFAULT_DURATION,
        indicators.MIN_DURATION, indicators.MAX_DURATION,
    )
    battery = ind["battery"]
    battery["low"] = max(1, min(100, battery["low"]))
    # A critical threshold at or above the low one would mean the low state
    # could never be reached; clamp rather than refuse, since this comes
    # from a file a person may have edited.
    battery["critical"] = max(1, min(battery["low"] - 1, battery["critical"])) \
        if battery["low"] > 1 else 1
    battery["repeat_critical"] = _number(
        battery["repeat_critical"], indicators.DEFAULT_CRITICAL_REPEAT, 30.0, 3600.0,
    )
    saver = out["battery_saver"]
    saver["brightness"] = max(0, min(zones.BRIGHTNESS_MAX, saver["brightness"]))
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

"""Named looks for the laptop keyboard, in four slots.

The external Firefly has had presets since the beginning (hyprutil/rgb/
presets.py); this is the same idea for the built-in keyboard, and the same
shape of file, so the two pages of the settings app behave alike.

A slot holds a whole look -- effect, four colours, brightness, speed -- plus
a name. Applying one writes it to current.json like any other change, so the
effects service picks it up and the keyboard follows with no extra plumbing.
"""
import json
import os
from pathlib import Path

from . import zones
from ..util import CONFIG_DIR, atomic_write_text

PRESET_SLOTS = [1, 2, 3, 4]

# Chosen to show off what the board can actually do rather than to be four
# shades of the same idea: one factory look, one that uses the new spatial
# blend, one organic, one that needs no colours at all.
DEFAULT_PRESETS = {
    1: {
        "name": "Omen",
        "effect": "static",
        "colors": list(zones.FACTORY_COLORS),
    },
    2: {
        "name": "Tide",
        "effect": "gradient",
        # Deep blue on the left running to cyan on the right; the middle two
        # are blended over, so only the outer pair really matter here.
        "colors": ["00e5ff", "0066ff", "0011aa", "0044cc"],
    },
    3: {
        "name": "Ember",
        "effect": "fire",
        "colors": ["ff4400", "ff6600", "ff2200", "ff8800"],
        "speed": 3,
    },
    4: {
        "name": "Spectrum",
        "effect": "wave",
        "colors": list(zones.FACTORY_COLORS),
        "speed": 4,
    },
}


def presets_dir():
    override = os.environ.get("HYPR_UTIL_CONFIG_DIR")
    return (Path(override) if override else CONFIG_DIR) / "kbd" / "presets"


def preset_path(slot):
    return presets_dir() / f"{slot}.json"


def _with_defaults(data, slot):
    look = zones.normalize_look(data)
    look["name"] = (data or {}).get("name") or DEFAULT_PRESETS.get(slot, {}).get(
        "name", f"Preset {slot}"
    )
    return look


def read(slot):
    """Slot `slot`, falling back to its factory contents."""
    try:
        return _with_defaults(json.loads(preset_path(slot).read_text()), slot)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _with_defaults(DEFAULT_PRESETS.get(slot, {}), slot)


def read_all():
    return {slot: read(slot) for slot in PRESET_SLOTS}


def write(slot, look, name=None):
    """Save a look into a slot, keeping the slot's name unless given one."""
    if slot not in PRESET_SLOTS:
        raise ValueError(f"preset slot must be one of {PRESET_SLOTS}")
    stored = zones.normalize_look(look)
    stored["name"] = name or (look or {}).get("name") or read(slot)["name"]
    directory = presets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(preset_path(slot), json.dumps(stored, indent=2))
    return stored


def rename(slot, name):
    look = read(slot)
    return write(slot, look, name=name)


def reset(slot):
    """Put a slot back to its factory contents."""
    try:
        preset_path(slot).unlink()
    except OSError:
        pass
    return read(slot)

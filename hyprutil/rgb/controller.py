"""Keyboard RGB control: capabilities, colour handling, and applying a look.

The protocol lives in device.py. This module owns the model the rest of the
app works in -- a "look", which is one of the twelve built-in effects with a
colour -- and translates it into device calls.

Colour, as the hardware actually defines it: the keyboard holds seven colour
slots, and an effect displays exactly ONE of them, chosen by a colour index.
Index 7 is special and means LOOP: the device runs its own rainbow and the
slots are ignored. There is no "cycle through my colours" mode; an earlier
version of this UI offered one, which is why picking several colours never
did what it looked like it should.
"""
import colorsys
import logging
import threading

from . import device
from . import watch

logger = logging.getLogger(__name__)

# Serializes every device call across threads in this process (live edits,
# flash, revert, preset apply, resume/reconnect restore, ...). Without it,
# two concurrent calls -- e.g. an older flash's revert racing a brand new
# profile-change flash that starts a moment later -- can interleave their
# messages on the wire, leaving the device in a "Frankenstein" state such as
# one call's effect combined with the other call's colours.
_device_lock = threading.Lock()

DEFAULT_BRIGHTNESS = device.DEFAULT_BRIGHTNESS
BRIGHTNESS_MAX = device.BRIGHTNESS_MAX
DEFAULT_SPEED = device.DEFAULT_SPEED
SPEED_MIN, SPEED_MAX = device.SPEED_MIN, device.SPEED_MAX
DIRECTION_RIGHT, DIRECTION_LEFT = device.DIRECTION_RIGHT, device.DIRECTION_LEFT
PROFILE_COUNT = device.PROFILE_COUNT

EFFECTS = [
    "static", "breathe", "fade", "getting_off", "little_stars", "laser",
    "wave", "neon", "raindrop", "ripple", "wave2", "swirl",
]

DEFAULT_COLORS = ["ff0000", "00ff00", "ffff00", "0000ff", "00ffff", "ff00ff", "ffffff"]

# Effects that ignore the colour entirely and animate their own, observed on
# the hardware. The vendor's own tables claim every effect takes a colour, so
# if one of these turns out to respond to a single colour after all, deleting
# it from this set is the whole change.
COLOURLESS_EFFECTS = {"little_stars", "wave", "neon", "raindrop", "wave2"}

# Direction is only meaningful for the two wave effects -- confirmed in the
# vendor software, which greys the control out for everything else.
DIRECTIONAL_EFFECTS = {"wave", "wave2"}

DEFAULT_LOOK = {
    "effect": "breathe",
    "color": DEFAULT_COLORS[0],
    "rainbow": False,
    "brightness": DEFAULT_BRIGHTNESS,
    "speed": DEFAULT_SPEED,
    "direction": DIRECTION_RIGHT,
}

_watcher = None
_listeners = []


def supports_color(effect):
    return effect not in COLOURLESS_EFFECTS


def supports_direction(effect):
    return effect in DIRECTIONAL_EFFECTS


def _on_watcher_change(connected):
    for cb in list(_listeners):
        try:
            cb(connected)
        except Exception:
            logger.exception("connection-change listener %r failed", cb)


def _ensure_watcher():
    global _watcher
    if _watcher is None:
        _watcher = watch.KeyboardWatcher(on_change=_on_watcher_change)
        _watcher.start()
    return _watcher


def is_connected():
    """Cheap, event-driven check -- no polling, no subprocess spawn."""
    return _ensure_watcher().connected


def on_connection_change(callback):
    """Register callback(connected: bool), invoked from udev's background
    thread. Consumers must hop back to their own toolkit's main loop inside
    the callback before touching UI."""
    _ensure_watcher()
    _listeners.append(callback)


def available():
    """True when the keyboard's control interface is present."""
    return device.available()


def ready():
    """True only when the device is reachable and plugged in."""
    return available() and is_connected()


def boost_color(hexval):
    """Push a color to full saturation/value (keeping its hue) before it goes
    to the device. A color picker rarely lands on pure saturated red/etc, and
    these LEDs render anything less than fully saturated as washed-out and
    pinkish -- this corrects that automatically, for any hue, every time."""
    hexval = hexval.lstrip("#")
    r, g, b = (int(hexval[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s > 0.05:  # leave near-grayscale colors (white etc) alone -- no hue to boost
        s = 1.0
    v = 1.0
    r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
    return f"{round(r2 * 255):02x}{round(g2 * 255):02x}{round(b2 * 255):02x}"


def hex_to_rgb(hexval):
    hexval = str(hexval).lstrip("#")
    return tuple(int(hexval[i:i + 2], 16) for i in (0, 2, 4))


def normalize_look(data):
    """Fill in and clamp a look read from disk or built by the UI."""
    look = dict(DEFAULT_LOOK)
    look.update({k: v for k, v in (data or {}).items() if k in DEFAULT_LOOK})
    if look["effect"] not in EFFECTS:
        look["effect"] = DEFAULT_LOOK["effect"]
    look["color"] = str(look["color"]).lstrip("#")
    look["rainbow"] = bool(look["rainbow"])
    look["brightness"] = min(max(int(look["brightness"]), 0), BRIGHTNESS_MAX)
    look["speed"] = min(max(int(look["speed"]), SPEED_MIN), SPEED_MAX)
    look["direction"] = DIRECTION_LEFT if look["direction"] else DIRECTION_RIGHT
    return look


def look_colors(look):
    """The colours that stand for a look, for swatches and menu icons.

    Empty means the look has no colour of its own to show -- an effect that
    animates its own.
    """
    look = normalize_look(look)
    if look["rainbow"]:
        return list(DEFAULT_COLORS)
    if supports_color(look["effect"]):
        return [look["color"]]
    return []


def apply_look(look, profile=None):
    """Put a look on the keyboard.

    `profile` selects one of the keyboard's own four profiles first, so the
    result is stored there and survives with no software running.
    """
    look = normalize_look(look)
    with _device_lock:
        colors = [hex_to_rgb(boost_color(look["color"]))] * 7
        color_idx = 7 if look["rainbow"] else 0
        device.apply(
            EFFECTS.index(look["effect"]), colors, color_idx,
            brightness=look["brightness"], speed=look["speed"],
            direction=look["direction"], profile=profile,
        )


def apply(effect, colors, color_idx=7, brightness=DEFAULT_BRIGHTNESS, speed=DEFAULT_SPEED):
    """Low-level apply, used by the flash notifications."""
    if not ready():
        raise RuntimeError("keyboard not connected")
    if effect not in EFFECTS:
        raise ValueError(f"unknown effect {effect!r}")
    if len(colors) != 7:
        raise ValueError("exactly 7 colors required")
    if not (0 <= color_idx <= 7):
        raise ValueError("color_idx must be 0-7")
    if not (0 <= brightness <= BRIGHTNESS_MAX):
        raise ValueError(f"brightness must be 0-{BRIGHTNESS_MAX}")
    if not (SPEED_MIN <= speed <= SPEED_MAX):
        raise ValueError(f"speed must be {SPEED_MIN}-{SPEED_MAX}")
    rgb_triples = [hex_to_rgb(boost_color(c)) for c in colors]
    with _device_lock:
        device.apply(EFFECTS.index(effect), rgb_triples, color_idx, brightness, speed)


def read_device_state():
    """What the keyboard says it is doing, or None if it cannot be reached.

    Used to notice changes made on the keyboard itself (its own Fn shortcuts
    cycle effect, brightness and profile) so the UI can follow them.
    """
    try:
        with _device_lock, device.opened() as fd:
            state = device.read_state(fd)
            state["profile"] = device.read_profile(fd)
            return state
    except (RuntimeError, OSError) as e:
        logger.debug("could not read keyboard state: %s", e)
        return None

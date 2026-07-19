"""Keyboard RGB device control, backed by the firefly-ctl binary (see
firefly-ctl/ next to this repo's root).

That binary has CAP_SYS_ADMIN granted via setcap, which raw USB control on
this device needs (detaching the kernel HID driver). Shelling out to it keeps
that privilege scoped to one narrow, single-purpose executable instead of
needing capability tricks around a general-purpose Python interpreter.
"""
import colorsys
import logging
import subprocess
import threading
import time
from pathlib import Path

from . import watch

logger = logging.getLogger(__name__)

# Serializes every apply() call across threads in this process (flash,
# revert, manual preset apply, resume/reconnect restore, ...). Without it,
# two concurrent calls -- e.g. an older flash's revert racing a brand new
# profile-change flash that starts a moment later -- can have their two
# firefly-ctl invocations (each itself sending header+color+effects, twice)
# land on the wire in an interleaved order, leaving the device in a
# "Frankenstein" state such as one call's effect combined with the other
# call's color, which nothing afterward ever corrects.
_device_lock = threading.Lock()

REPO_DIR = Path(__file__).resolve().parents[2]
CTL_BIN = REPO_DIR / "firefly-ctl" / "target" / "debug" / "firefly-ctl"
DEFAULT_BRIGHTNESS = 5  # the device's own default of 1 renders washed out

EFFECTS = [
    "static", "breathe", "fade", "getting_off", "little_stars", "laser",
    "wave", "neon", "raindrop", "ripple", "wave2", "swirl",
]

DEFAULT_COLORS = ["ff0000", "00ff00", "ffff00", "0000ff", "00ffff", "ff00ff", "ffffff"]

_watcher = None
_listeners = []


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
    return CTL_BIN.exists()


def ready():
    """True only when both the control binary exists and the device is plugged in."""
    return available() and is_connected()


def _boost_color(hexval):
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


def apply(effect, colors, color_idx=7, brightness=DEFAULT_BRIGHTNESS):
    if not ready():
        raise RuntimeError("keyboard not connected")
    if effect not in EFFECTS:
        raise ValueError(f"unknown effect {effect!r}")
    if len(colors) != 7:
        raise ValueError("exactly 7 colors required")
    if not (0 <= color_idx <= 7):
        raise ValueError("color_idx must be 0-7")
    if not (0 <= brightness <= 255):
        raise ValueError("brightness must be 0-255")
    colors = [_boost_color(c) for c in colors]
    cmd = [
        str(CTL_BIN),
        "--effect", effect,
        "--ci", str(color_idx),
        "--brightness", str(brightness),
        "--colors", ",".join(colors),
    ]
    with _device_lock:
        # The device occasionally drops the effect switch mid-transition
        # (e.g. going from a multi-color animation to a different static
        # color); a second send a moment later reliably settles it.
        subprocess.run(cmd, check=True, capture_output=True, timeout=5)
        time.sleep(0.15)
        subprocess.run(cmd, check=True, capture_output=True, timeout=5)

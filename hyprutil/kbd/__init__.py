"""The laptop's own keyboard lighting: four zones, through the BIOS mailbox.

This is the HP Omen keyboard built into the machine, not the external
Firefly (that one is USB HID and lives in hyprutil/rgb/). The two are
deliberately separate: they share no protocol, no state and no failure mode,
and the only thing they have in common is that both are keyboards.

Everything here goes through the root daemon in service.py, because the
mailbox is root-only. Calls fall back to driving the hardware directly when
this process is already root, which is what makes `sudo hyprutil kbd probe`
work on a machine where the daemon is not installed yet.
"""
import os

from . import service, state, zones
from .mailbox import BiosError, MailboxError, MailboxUnavailable
from .service import ServiceUnavailable
from .state import read_current, write_current
from .zones import (
    ANIMATED,
    BRIGHTNESS_MAX,
    DEFAULT_BRIGHTNESS,
    DEFAULT_LOOK,
    DEFAULT_SPEED,
    DISPLAY_ORDER,
    EFFECTS,
    EFFECT_LABELS,
    FACTORY_COLORS,
    SPEED_MAX,
    SPEED_MIN,
    ZONE_LEFT,
    ZONE_MIDDLE,
    ZONE_NAMES,
    ZONE_RIGHT,
    ZONE_WASD,
    frame_colors,
    normalize_look,
    to_hex,
    to_rgb,
    zone_colors,
)

__all__ = [
    "ANIMATED", "BRIGHTNESS_MAX", "DEFAULT_BRIGHTNESS", "DEFAULT_LOOK", "DEFAULT_SPEED",
    "DISPLAY_ORDER", "EFFECTS", "EFFECT_LABELS", "FACTORY_COLORS", "SPEED_MAX", "SPEED_MIN",
    "ZONE_LEFT", "ZONE_MIDDLE", "ZONE_NAMES", "ZONE_RIGHT", "ZONE_WASD",
    "BiosError", "MailboxError", "MailboxUnavailable", "ServiceUnavailable",
    "apply", "available", "frame_colors", "normalize_look", "probe", "read_current",
    "service", "state", "status", "to_hex", "to_rgb", "write_current", "zone_colors",
    "zones",
]

_direct = None


def _root_backend():
    """A Daemon driven in-process, for a root CLI with no service running."""
    global _direct
    if _direct is None:
        _direct = service.Daemon()
    return _direct


def _ask(op, **fields):
    """One request, through the daemon where there is one."""
    client = service.Client()
    try:
        return client.request(op, **fields)
    except ServiceUnavailable:
        if os.geteuid() != 0:
            raise
        return _root_backend().handle({"op": op, **fields})


def status():
    """What the daemon knows: the keyboard, the look, whether it is lit.

    Raises ServiceUnavailable when the daemon is not reachable -- callers
    that only want to know whether to show a keyboard page should use
    available() instead.
    """
    return _ask("status")


def probe():
    """Diagnostics: transport, keyboard type, colour table, backlight."""
    return _ask("probe")


def available():
    """True when there is a four-zone keyboard here that we can drive."""
    try:
        reply = status()
    except ServiceUnavailable:
        return False
    keyboard = reply.get("keyboard")
    return bool(keyboard and keyboard.get("usable"))


def apply(look):
    """Save a look and put it on the keyboard.

    Saving first is deliberate: the file is what the daemon reads back after
    a resume or a restart, so a look that reached the hardware but not the
    disk would quietly revert the next time the machine woke up.
    """
    look = write_current(look)
    reply = _ask("apply", look=look)
    if not reply.get("ok"):
        raise MailboxError(reply.get("error") or "the keyboard refused the change")
    return reply


def reload():
    """Re-apply the saved look (after a resume, or a service restart)."""
    return _ask("reload")

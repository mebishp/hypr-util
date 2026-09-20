"""The laptop keyboard, as the hyprkbd driver presents it.

Everything the keyboard's lighting understands lives behind one ACPI-WMI
method, and calling that method from userspace means root. The hyprkbd
kernel module (kernel/hyprkbd/ in this repo) does the call instead and
publishes the result as ordinary files:

    /sys/devices/platform/hyprkbd/
        keyboard_type   the firmware's type byte
        type_name       what that byte means, in words
        zones           how many zones are wired up (1 or 4)
        kind            none | zones | perkey
        supported       the firmware's own "lighting supported" bit
        colors          "rrggbb rrggbb rrggbb rrggbb", every zone at once
        zone0..zone3    one zone each
        backlight       0 or 1
        backlight_raw   the raw byte, for diagnostics
        refresh         write 1 to re-read the firmware's table

A udev rule hands the writable ones to the desktop user at install time, so
nothing above this module needs to be root, hold a lock, or talk to a
daemon. This file is therefore just open(), read() and write().

`colors` is the one to reach for when more than one zone changes: the driver
holds the firmware's 128-byte table and sends it once, so a whole frame of
an animation costs a single WMI call. Writing zone0..zone3 one after another
would cost four.
"""
import os
from pathlib import Path

SYSFS = Path("/sys/devices/platform/hyprkbd")
MODULE = "hyprkbd"


class DeviceError(RuntimeError):
    """The driver is there and refused, or the firmware did."""


class DeviceUnavailable(DeviceError):
    """The hyprkbd module is not loaded, or this is not an HP laptop."""


def present():
    """True when the driver is loaded and has published its attributes."""
    return SYSFS.is_dir()


def writable():
    """True when this process may actually change the lighting.

    False on a machine where the udev rule never ran -- which is what
    separates "you need to finish installing" from "your keyboard cannot do
    this".
    """
    return present() and os.access(SYSFS / "colors", os.W_OK)


def explain():
    if not present():
        return (
            "the hyprkbd kernel module is not loaded "
            "(run ./setup.sh to build and install it, or 'sudo modprobe hyprkbd')"
        )
    return (
        f"{SYSFS}/colors is not writable by this user "
        "(the udev rule that hands it over did not run; re-run ./setup.sh)"
    )


def read_attribute(name):
    """One sysfs attribute, as text."""
    try:
        return (SYSFS / name).read_text().strip()
    except FileNotFoundError as e:
        raise DeviceUnavailable(explain()) from e
    except PermissionError as e:
        raise DeviceUnavailable(explain()) from e
    except OSError as e:
        # A read that reaches the driver and fails there is the firmware
        # refusing, not the transport missing -- worth telling apart.
        raise DeviceError(f"{name}: {e.strerror or e}") from e


def _write(name, value):
    try:
        # One write() per value: sysfs parses each write as a whole request,
        # and a buffered stream could split it.
        fd = os.open(str(SYSFS / name), os.O_WRONLY)
        try:
            os.write(fd, str(value).encode())
        finally:
            os.close(fd)
    except FileNotFoundError as e:
        raise DeviceUnavailable(explain()) from e
    except PermissionError as e:
        raise DeviceUnavailable(explain()) from e
    except OSError as e:
        raise DeviceError(f"{name}: {e.strerror or e}") from e


def read_attribute_int(name, default=0):
    try:
        return int(read_attribute(name), 0)
    except (DeviceError, ValueError):
        return default


# -- what keyboard is this --

def keyboard():
    """What the driver found, or None when there is nothing to drive.

    The driver has already asked the firmware both questions that matter --
    what the layout is, and whether the colour table reads back -- so this
    is a read of its answer rather than a probe of its own.
    """
    if not present():
        return None
    kind = read_attribute("kind")
    if kind == "none":
        return None
    zones = read_attribute_int("zones")
    type_byte = read_attribute_int("keyboard_type")
    return {
        "type": type_byte,
        "type_name": read_attribute("type_name"),
        "kind": kind,
        "zones": zones,
        "numpad": type_byte in (1, 4),
        "declared": bool(read_attribute_int("supported")),
        # Per-key boards answer every one of these calls and drive nothing:
        # 128 bytes cannot address 176 keys. Reported, not offered.
        "usable": kind == "zones",
        "describe": (
            "per-key (not driveable through the firmware)" if kind == "perkey"
            else "1 zone" if zones == 1
            else f"{zones} zones"
        ),
    }


# -- colours and backlight --

def read_colors():
    """The zone colours the firmware is holding, as hex strings."""
    return read_attribute("colors").split()


def write_colors(colors):
    """Set every zone at once -- one WMI call, whatever the zone count."""
    _write("colors", " ".join(colors))


def read_backlight():
    return bool(read_attribute_int("backlight"))


def write_backlight(on):
    _write("backlight", 1 if on else 0)


def backlight_raw():
    return read_attribute_int("backlight_raw")


def refresh():
    """Make the driver re-read the firmware's table."""
    _write("refresh", 1)

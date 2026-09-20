"""What the keyboard reacts to: power profile, battery, and CPU load.

Everything here is a small file in /sys or /proc, read on a timer. That is
deliberate -- this module is imported by the effects service, which is a
plain user process with no GLib loop, and dragging PyGObject and a D-Bus
proxy into it to learn one string would be a poor trade. The rest of the app
watches power-profiles-daemon over D-Bus (see hyprutil/power.py) because it
needs to be told the instant a profile changes; an indicator that follows
within a second or two does not.

`/sys/firmware/acpi/platform_profile` is the hardware's own idea of the
profile and is what power-profiles-daemon actually writes, so it stays
correct on a machine that is not running ppd at all. Its vocabulary differs
by one word -- it says "low-power" where ppd says "power-saver" -- so the
ppd names are used here and the sysfs one is translated on the way in,
because those are the names the rest of the project already speaks.
"""
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
POWER_SUPPLY = Path("/sys/class/power_supply")
PROC_STAT = Path("/proc/stat")

# platform_profile's name -> the name power-profiles-daemon uses, which is
# what hyprutil/rgb/notify.py already keys its colours by.
PROFILE_ALIASES = {
    "low-power": "power-saver",
    "quiet": "power-saver",
    "cool": "power-saver",
}
PROFILES = ("power-saver", "balanced", "performance")

# How stale a reading may be before it is taken again. The effects loop ticks
# every 120 ms while animating, and none of this changes that fast.
REFRESH_SECONDS = 2.0


def _read(path, default=None):
    try:
        return path.read_text().strip()
    except OSError:
        return default


def power_profile():
    """The active power profile, in power-profiles-daemon's vocabulary."""
    raw = _read(PLATFORM_PROFILE)
    if not raw:
        return None
    return PROFILE_ALIASES.get(raw, raw)


def _battery_dir():
    """The first real battery. Laptops have one; the USB-C power supplies
    that also appear here are not batteries and must not be mistaken for a
    flat one."""
    try:
        entries = sorted(POWER_SUPPLY.iterdir())
    except OSError:
        return None
    for entry in entries:
        if _read(entry / "type") == "Battery" and (entry / "capacity").exists():
            return entry
    return None


def _on_ac():
    """True when a mains adapter is plugged in.

    Read from the adapter rather than inferred from the battery's status,
    because "Full" and "Not charging" both happen on AC and neither says so.
    """
    try:
        entries = sorted(POWER_SUPPLY.iterdir())
    except OSError:
        return None
    for entry in entries:
        if _read(entry / "type") == "Mains":
            online = _read(entry / "online")
            if online is not None:
                return online == "1"
    return None


class Telemetry:
    """A cached view of the machine, cheap enough to ask for every frame.

    One instance per process. Readings older than REFRESH_SECONDS are taken
    again; everything else is handed back from the last sample, so an effect
    running at eight frames a second does not read /proc/stat eight times.
    """

    def __init__(self, refresh=REFRESH_SECONDS):
        self.refresh = refresh
        self._taken = 0.0
        self._sample = {
            "profile": None, "battery": None, "charging": None,
            "on_ac": None, "load": 0.0,
        }
        self._cpu_prev = None

    def _cpu_load(self):
        """Busy fraction since the previous call, from /proc/stat.

        The first call has nothing to compare against and reports 0.0 rather
        than the since-boot average, which would be a meaningless number to
        light a keyboard with.
        """
        line = _read(PROC_STAT, "")
        if not line.startswith("cpu "):
            return 0.0
        try:
            fields = [int(v) for v in line.split("\n", 1)[0].split()[1:]]
        except ValueError:
            return 0.0
        if len(fields) < 4:
            return 0.0
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        previous, self._cpu_prev = self._cpu_prev, (idle, total)
        if previous is None:
            return 0.0
        idle_delta = idle - previous[0]
        total_delta = total - previous[1]
        if total_delta <= 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - idle_delta / total_delta))

    def sample(self, force=False):
        now = time.monotonic()
        if not force and now - self._taken < self.refresh:
            return self._sample
        self._taken = now

        battery = charging = None
        entry = _battery_dir()
        if entry is not None:
            raw = _read(entry / "capacity")
            if raw is not None:
                try:
                    battery = max(0, min(100, int(raw)))
                except ValueError:
                    battery = None
            charging = _read(entry / "status") == "Charging"

        self._sample = {
            "profile": power_profile(),
            "battery": battery,
            "charging": charging,
            "on_ac": _on_ac(),
            "load": self._cpu_load(),
        }
        return self._sample


# A process-wide default, so the CLI and the service share one /proc/stat
# baseline rather than each reporting 0.0 on their first look.
_shared = None


def telemetry(force=False):
    global _shared
    if _shared is None:
        _shared = Telemetry()
    return _shared.sample(force=force)

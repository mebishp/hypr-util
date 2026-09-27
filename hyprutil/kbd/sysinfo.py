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


def _find_supply(kind, needs=None):
    """The first power supply of a kind, or None.

    Laptops have one battery; the USB-C power supplies that also appear here
    are not batteries and must not be mistaken for a flat one, which is what
    the `needs` check is for.
    """
    try:
        entries = sorted(POWER_SUPPLY.iterdir())
    except OSError:
        return None
    for entry in entries:
        if _read(entry / "type") != kind:
            continue
        if needs is None or (entry / needs).exists():
            return entry
    return None


def battery_dir():
    return _find_supply("Battery", needs="capacity")


def mains_dir():
    # `online` is the only thing we ask an adapter, so an adapter that does
    # not publish it is not the one we are looking for -- keep walking.
    return _find_supply("Mains", needs="online")


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
        # Which directory under /sys/class/power_supply is the battery and
        # which is the adapter, remembered once. Finding them means listing
        # the directory and reading a `type` file per entry, and neither
        # answer changes while the machine is running -- doing that twice a
        # second for a number that moves by 1% an hour was most of what this
        # class spent its time on. Re-resolved if the directory goes away,
        # which is the one case where it can change (a hot-plugged supply,
        # or a suspend that renumbers them).
        self._battery = None
        self._mains = None

    def _supply(self, attr, finder):
        cached = getattr(self, attr)
        if cached is not None and cached.exists():
            return cached
        found = finder()
        setattr(self, attr, found)
        return found

    def _cpu_load(self):
        """Busy fraction since the previous call, from /proc/stat.

        The first call has nothing to compare against and reports 0.0 rather
        than the since-boot average, which would be a meaningless number to
        light a keyboard with.
        """
        # Only the first line is wanted, and /proc/stat is a long file on a
        # machine with many cores -- one line per CPU plus the interrupt
        # table. Reading the whole thing to parse its first 60 bytes is the
        # kind of waste that happens every half second under the CPU meter.
        try:
            with open(PROC_STAT, "rb") as f:
                line = f.readline().decode()
        except OSError:
            return 0.0
        if not line.startswith("cpu "):
            return 0.0
        try:
            fields = [int(v) for v in line.split()[1:]]
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
        entry = self._supply("_battery", battery_dir)
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
            "on_ac": self._on_ac(),
            "load": self._cpu_load(),
        }
        return self._sample

    def _on_ac(self):
        """True when a mains adapter is plugged in.

        Read from the adapter rather than inferred from the battery's status,
        because "Full" and "Not charging" both happen on AC and neither says
        so.
        """
        entry = self._supply("_mains", mains_dir)
        if entry is None:
            return None
        online = _read(entry / "online")
        return None if online is None else online == "1"


# A process-wide default, so the CLI and the service share one /proc/stat
# baseline rather than each reporting 0.0 on their first look.
_shared = None


def telemetry(force=False):
    global _shared
    if _shared is None:
        _shared = Telemetry()
    return _shared.sample(force=force)

"""Shared D-Bus helpers so the tray and the automation daemon can react to
power-profile and systemd-unit state without forking a subprocess
(`powerprofilesctl`/`systemctl`) on every poll tick.
"""
import logging
import threading

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

# Tried in order; power-profiles-daemon has exposed its interface under both
# names across versions (net.hadess.* is the historical name, still shipped
# alongside the freedesktop one for compatibility).
CANDIDATES = [
    ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles"),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles"),
]
POLL_INTERVAL_MS = 2000

_UNSET = object()


class ProfileWatcher:
    """Watches the active power profile over D-Bus (falling back to polling
    `fan.current_power_profile()` if power-profiles-daemon isn't reachable
    at all) and calls `on_change(profile, is_initial)` on the GLib main
    thread every time it's observed to differ from the last-seen value.

    `is_initial` is True for the very first observation (establishing a
    baseline, not a change the user made) and False for every actual change
    after that.

    Runs on whichever thread is pumping the process's (default) GLib main
    context; callers on another toolkit's main loop (Qt, GTK) must hop back
    to it themselves inside on_change, same as rgb/watch.py's udev callback.
    """

    def __init__(self, on_change):
        self._on_change = on_change
        self._last_profile = _UNSET
        # Kept alive here -- if `proxy` were only a local variable in
        # _start(), nothing would hold the underlying GDBusProxy once this
        # method returns, so Python would garbage-collect it and silently
        # drop the signal subscription along with it.
        self._proxy = None
        self._start()

    def _start(self):
        for bus_name, obj_path, iface in CANDIDATES:
            try:
                proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
                    bus_name, obj_path, iface, None,
                )
            except GLib.Error:
                continue
            # Construction succeeds even if the bus name has no owner; only
            # a populated property cache proves the service actually answered.
            value = proxy.get_cached_property("ActiveProfile")
            if value is None:
                continue
            self._proxy = proxy
            proxy.connect("g-properties-changed", self._on_properties_changed)
            logger.info("watching power profile via %s", bus_name)
            self._handle(value.unpack())
            return
        logger.warning("power-profiles-daemon not reachable over D-Bus, falling back to polling")
        GLib.timeout_add(POLL_INTERVAL_MS, self._poll)
        self._poll()

    def _poll(self):
        def worker():
            from . import fan
            GLib.idle_add(self._handle, fan.current_power_profile())

        threading.Thread(target=worker, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _on_properties_changed(self, proxy, changed_properties, invalidated_properties):
        changed = changed_properties.unpack()
        if "ActiveProfile" in changed:
            self._handle(changed["ActiveProfile"])

    def _handle(self, profile):
        is_initial = self._last_profile is _UNSET
        if profile == self._last_profile:
            return
        self._last_profile = profile
        self._on_change(profile, is_initial)


def service_active(unit_name):
    """Whether a systemd unit is active, via the system bus -- no
    `systemctl` subprocess fork."""
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        (unit_path,) = conn.call_sync(
            "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "GetUnit",
            GLib.Variant("(s)", (unit_name,)), None, Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()
        (active_state,) = conn.call_sync(
            "org.freedesktop.systemd1", unit_path,
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.freedesktop.systemd1.Unit", "ActiveState")),
            None, Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()
        return active_state == "active"
    except GLib.Error:
        return False

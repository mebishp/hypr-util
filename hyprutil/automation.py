"""Always-on background automation for hypr-util.

Owns the reactive behavior that used to live only in the tray's 2-second
poll loop -- following power-profile changes to set the display refresh
rate, and re-syncing it after a resume from sleep. Runs as a user-session
systemd service (see system/hypr-util-daemon.service) independent of
whether the tray or settings app happen to be running, so profile changes
made from GNOME Settings, powerprofilesctl, or the GTK app are still
reflected in the refresh rate.

The keyboard's own lighting is not restored here: it has its own units
(hypr-util-kbd.service for boot and resume, hypr-util-kbd-effects.service
for the animation), so nothing in this daemon needs to touch it.
"""
import logging
import threading

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from . import display
from . import fan
from . import power

logger = logging.getLogger(__name__)


class Automation:
    def __init__(self):
        self._loop = GLib.MainLoop()

    def run(self):
        # Stored on self -- an unreferenced ProfileWatcher forms a reference
        # cycle with its own D-Bus proxy (proxy's signal closure keeps the
        # watcher's bound method alive, watcher keeps the proxy alive via
        # self._proxy) that has no external root, so Python's cyclic GC will
        # eventually collect it and silently kill the subscription.
        self._profile_watcher = power.ProfileWatcher(self._on_profile)
        self._start_sleep_watch()

        logger.info("automation daemon started")
        self._loop.run()

    # -- power profile --

    def _on_profile(self, profile, is_initial):
        target_hz = fan.PROFILE_REFRESH_HZ.get(profile)
        if target_hz is None:
            return
        if not is_initial:
            logger.info("power profile changed to %r", profile)
        # Off the GLib thread: set_refresh_rate shells out to the compositor,
        # which must not stall the loop that feeds this callback.
        threading.Thread(
            target=display.set_refresh_rate, args=(target_hz,), daemon=True
        ).start()

    def _sync_refresh_rate_now(self):
        """One-off resync after a resume."""
        profile = fan.current_power_profile()
        target_hz = fan.PROFILE_REFRESH_HZ.get(profile)
        if target_hz is not None:
            display.set_refresh_rate(target_hz)

    # -- resume from sleep --

    def _start_sleep_watch(self):
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            logger.warning("could not connect to system bus for logind sleep signal")
            return
        conn.signal_subscribe(
            "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
            "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self._on_prepare_for_sleep,
        )

    def _on_prepare_for_sleep(self, connection, sender, path, iface, signal, params):
        (going_to_sleep,) = params.unpack()
        if going_to_sleep:
            return
        logger.info("resumed, re-syncing the refresh rate")
        threading.Thread(target=self._sync_refresh_rate_now, daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fan.ensure_config_defaults()
    Automation().run()


if __name__ == "__main__":
    main()

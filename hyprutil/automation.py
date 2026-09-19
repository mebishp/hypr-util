"""Always-on background automation for hypr-util.

Owns the reactive behavior that used to live only in the tray's 2-second
poll loop -- following power-profile changes (display refresh rate + RGB
flash) and restoring keyboard lighting after it comes back (USB reconnect,
resume from sleep, or this daemon's own startup). Runs as a user-session
systemd service (see system/hypr-util-daemon.service) independent of
whether the tray or settings app happen to be running, so profile changes
made from GNOME Settings, powerprofilesctl, or the GTK app are still
reflected in refresh rate and RGB.
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
from . import rgb

logger = logging.getLogger(__name__)


class _FlashCoordinator:
    """Serializes RGB 'flash' sessions (apply color -> wait -> maybe revert)
    so that:

    1. A stale session's device writes can never land on the wire after a
       newer session's (and its correct revert) has already finished.
    2. Two sessions never attempt concurrent device writes at all (this
       complements, rather than replaces, the lower-level `_device_lock` in
       rgb/controller.py).

    A rapid double profile switch is the case this protects: the older
    switch's revert must not stomp the newer switch's colors.
    """

    def __init__(self):
        self._generation_lock = threading.Lock()
        self._generation = 0
        self._session_lock = threading.Lock()

    def start(self):
        """Call synchronously, before spawning the worker thread that will
        actually flash -- see the comment on the old _on_profile for why
        this can't be assigned from inside the thread itself."""
        with self._generation_lock:
            self._generation += 1
            return self._generation

    def _current(self):
        with self._generation_lock:
            return self._generation

    def run(self, generation, flash_fn):
        """Call from a worker thread. flash_fn(revert_predicate) should
        perform the actual rgb.flash*() call, passing revert_predicate
        through as its `revert=` argument."""
        with self._session_lock:
            if generation != self._current():
                return  # superseded while queued behind this lock
            try:
                flash_fn(lambda: generation == self._current())
            except Exception:
                logger.exception("RGB flash failed")


class Automation:
    def __init__(self):
        self._loop = GLib.MainLoop()
        self._flasher = _FlashCoordinator()

    def run(self):
        # Stored on self -- an unreferenced ProfileWatcher forms a reference
        # cycle with its own D-Bus proxy (proxy's signal closure keeps the
        # watcher's bound method alive, watcher keeps the proxy alive via
        # self._proxy) that has no external root, so Python's cyclic GC will
        # eventually collect it and silently kill the subscription.
        self._profile_watcher = power.ProfileWatcher(self._on_profile)
        self._start_sleep_watch()
        rgb.on_connection_change(self._on_keyboard_change)
        threading.Thread(target=self._restore_rgb, args=("startup",), daemon=True).start()

        logger.info("automation daemon started")
        self._loop.run()

    # -- power profile --

    def _on_profile(self, profile, is_initial):
        target_hz = fan.PROFILE_REFRESH_HZ.get(profile)
        if is_initial:
            # Silently align refresh rate at startup -- this is a sync, not
            # a change the user made, so no flash.
            if target_hz is not None:
                display.set_refresh_rate(target_hz)
            return
        logger.info("power profile changed to %r", profile)
        generation = self._flasher.start()
        threading.Thread(
            target=self._apply_profile, args=(profile, target_hz, generation), daemon=True
        ).start()

    def _apply_profile(self, profile, target_hz, generation):
        if target_hz is not None:
            display.set_refresh_rate(target_hz)
        if not rgb.ready():
            return
        self._flasher.run(generation, lambda revert: rgb.flash_for_profile(profile, revert=revert))
        logger.info("flashed RGB for profile %r", profile)

    def _sync_refresh_rate_now(self):
        """One-off resync after resume/reconnect, without a flash."""
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
        threading.Thread(target=self._restore, args=("resume",), daemon=True).start()

    # -- keyboard reconnect --

    def _on_keyboard_change(self, connected):
        # Called from udev's monitor thread, not the GLib main loop.
        if connected:
            threading.Thread(target=self._restore_rgb, args=("keyboard reconnect",), daemon=True).start()

    # -- shared restore path --

    def _restore(self, reason):
        logger.info("restoring state (%s)", reason)
        self._sync_refresh_rate_now()
        self._restore_rgb(reason)

    def _restore_rgb(self, reason):
        if not rgb.ready():
            return
        slot = rgb.active_preset()
        try:
            if slot is not None:
                rgb.apply_preset(slot)
                logger.info("restored RGB preset %r (%s)", slot, reason)
            else:
                # A hand-edited look is still what the keyboard should come
                # back to -- restoring only saved presets left those looks
                # lost after a resume.
                rgb.apply_current(rgb.read_current())
                logger.info("restored current RGB look (%s)", reason)
        except Exception:
            logger.exception("failed to restore RGB (%s)", reason)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fan.ensure_config_defaults()
    rgb.ensure_defaults()
    Automation().run()


if __name__ == "__main__":
    main()

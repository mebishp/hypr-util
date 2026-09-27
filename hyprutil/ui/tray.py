"""Tray app for the hypr-util custom fan curve daemon.

Quick status and actions only -- full curve editing, RGB configuration, and
log viewing live in the settings app (launched from here via "Open hypr-util...").
"""
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .. import fan as core
from .. import kbd
from .. import power

logger = logging.getLogger(__name__)

# The installed launcher, resolved from PATH -- the tray may be running from
# an installed copy (<prefix>/lib/hypr-util) or straight from a checkout, so
# deriving this from __file__ would point at the wrong place in one of those
# two layouts. Only used for the Popen fallback in open_app().
APP_LAUNCHER = shutil.which("hyprutil") or "hyprutil"


def make_icon(temp):
    if temp is None:
        color = QColor("gray")
    elif temp < 50:
        color = QColor("#4caf50")
    elif temp < 70:
        color = QColor("#ffb300")
    else:
        color = QColor("#e53935")

    # The tray (GNOME's AppIndicator extension) renders every indicator's
    # icon at one uniform pixel size -- there's no per-app size override, so
    # the only lever here is filling that fixed slot edge-to-edge with no
    # transparent margin, which reads as bigger/bolder than an icon with
    # padding even at the same pixel footprint.
    pixmap = QPixmap(128, 128)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(QColor(0, 0, 0, 0))
    painter.drawRoundedRect(0, 0, 64, 64, 14, 14)
    painter.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPointSize(30)
    painter.setFont(font)
    label = str(int(temp)) if temp is not None else "?"
    # QPainter operates in logical (device-independent) coordinates once the
    # pixmap's devicePixelRatio is set, so the text rect must use the
    # logical 64x64 size, not pixmap.rect()'s raw 128x128 device pixels.
    painter.drawText(0, 0, 64, 64, 0x84, label)
    painter.end()
    return pixmap


class FanTray(QSystemTrayIcon):
    # service_active() is a D-Bus round trip; gathering it (plus the hwmon
    # reads) happens on a worker thread and this signal hands the results
    # back to the Qt main thread, so the 2-second poll never blocks the UI.
    _status_ready = pyqtSignal(dict)
    # power.ProfileWatcher's callback fires on the background GLib loop
    # thread (see _start_glib_loop below), not the Qt thread.
    _profile_changed = pyqtSignal(str)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._refreshing = False
        self._current_profile = None
        self._status_ready.connect(self._apply_status)
        self._profile_changed.connect(self._on_profile_changed)
        self._start_glib_loop()
        self.menu = QMenu()
        # Qt menus don't keep Python-side references to dynamically created
        # QActions; without holding onto them ourselves they get garbage
        # collected and their triggered() signals silently stop firing.
        self._actions = []

        self.status_action = self._make_action("Status: loading...", enabled=False)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()

        self.profile_actions = {}
        profile_menu = QMenu("Power Profile")
        for p in core.PROFILES:
            act = self._make_action(
                core.PROFILE_LABELS[p], checkable=True, slot=lambda checked, prof=p: core.set_power_profile(prof)
            )
            profile_menu.addAction(act)
            self.profile_actions[p] = act
        self.menu.addMenu(profile_menu)
        self._actions.append(profile_menu)

        self.menu.addSeparator()
        # Four zones behind the BIOS mailbox, reached through the hyprkbd
        # driver's sysfs files. Disabled until the driver answers, so a click
        # cannot silently do nothing.
        self.kbd_menu = QMenu("Keyboard")
        self.kbd_menu.setEnabled(False)
        self.kbd_menu.addAction(
            self._make_action("Backlight on", slot=lambda checked: self.set_kbd(on=True))
        )
        self.kbd_menu.addAction(
            self._make_action("Backlight off", slot=lambda checked: self.set_kbd(on=False))
        )

        # Presets before effects: a preset is a whole look someone chose and
        # named, which is more often what they want from a tray than an
        # effect applied over whatever colours happen to be set.
        self.kbd_menu.addSeparator()
        presets_menu = self.kbd_menu.addMenu("Presets")
        for slot, look in kbd.presets.read_all().items():
            presets_menu.addAction(self._make_action(
                look["name"],
                slot=lambda checked, n=slot: self.apply_kbd_preset(n),
            ))

        effects_menu = self.kbd_menu.addMenu("Effects")
        for effect in kbd.EFFECTS:
            effects_menu.addAction(self._make_action(
                kbd.EFFECT_LABELS[effect],
                slot=lambda checked, e=effect: self.set_kbd(effect=e),
            ))
        self.menu.addMenu(self.kbd_menu)
        self._actions.append(self.kbd_menu)

        self.menu.addSeparator()
        open_app_action = self._make_action("Open hypr-util...", slot=self.open_app)
        self.menu.addAction(open_app_action)

        self.menu.addSeparator()
        quit_action = self._make_action("Quit Tray", slot=app.quit)
        self.menu.addAction(quit_action)

        self.setContextMenu(self.menu)
        self.setVisible(True)

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def _start_glib_loop(self):
        # power.ProfileWatcher needs a GLib main context actively iterating
        # to ever deliver its D-Bus signal; Qt doesn't pump one, so run a
        # dedicated one on a background thread. The watcher itself is
        # created here too (not in __init__) and stored on self -- an
        # unreferenced watcher forms a reference cycle with its own D-Bus
        # proxy that Python's cyclic GC will eventually collect, silently
        # dropping the subscription (see hyprutil/power.py and the same
        # lesson learned in automation.py).
        def run_loop():
            self._profile_watcher = power.ProfileWatcher(
                lambda profile, is_initial: self._profile_changed.emit(profile)
            )
            GLib.MainLoop().run()

        self._glib_thread = threading.Thread(target=run_loop, daemon=True)
        self._glib_thread.start()

    def _on_profile_changed(self, profile):
        self._current_profile = profile
        for p, act in self.profile_actions.items():
            act.setChecked(p == profile)
        self._update_status_text()

    def _make_action(self, label, slot=None, enabled=True, checkable=False):
        act = QAction(label)
        act.setEnabled(enabled)
        if checkable:
            act.setCheckable(True)
        if slot is not None:
            act.triggered.connect(slot)
        self._actions.append(act)
        return act

    def open_app(self):
        # D-Bus-activate the resident settings app (see
        # hyprutil/ui/app.py + system/org.hyprnon.hyprutil.service) instead
        # of spawning a fresh interpreter -- if it's already running this
        # just re-presents its window; if not, the bus starts it per the
        # installed .service file. Falls back to a plain Popen if the
        # service file isn't installed yet (e.g. setup.sh hasn't been
        # re-run since this D-Bus activation support was added).
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            conn.call_sync(
                "org.hyprnon.hyprutil", "/org/hyprnon/hyprutil",
                "org.freedesktop.Application", "Activate",
                GLib.Variant("(a{sv})", ({},)), None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error:
            subprocess.Popen([APP_LAUNCHER, "app"])

    def set_kbd(self, on=None, effect=None):
        threading.Thread(
            target=self._set_kbd_worker, args=(on, effect), daemon=True
        ).start()

    def apply_kbd_preset(self, slot):
        threading.Thread(
            target=self._kbd_preset_worker, args=(slot,), daemon=True
        ).start()

    def _kbd_preset_worker(self, slot):
        try:
            kbd.apply_preset(slot)
        except Exception:
            logger.exception("failed to apply laptop keyboard preset %r", slot)

    def _set_kbd_worker(self, on, effect):
        try:
            look = dict(kbd.read_current())
            if on is not None:
                look["on"] = on
            if effect is not None:
                # Picking an effect with the backlight off is asking to see
                # it, not to set it up for later.
                look["effect"] = effect
                look["on"] = True
            kbd.apply(look)
        except Exception:
            logger.exception("failed to change the laptop keyboard lighting")

    def refresh(self):
        # power.service_active() is a D-Bus round trip (not a subprocess
        # fork, but still I/O); gather it off the main thread, skipping if
        # the previous gather is still in flight. The power profile itself
        # is no longer polled here at all -- see _on_profile_changed, fed by
        # the event-driven power.ProfileWatcher.
        if self._refreshing:
            return
        self._refreshing = True
        threading.Thread(target=self._gather_status, daemon=True).start()

    def _gather_status(self):
        # Reacting to power-profile changes (display refresh rate) is handled
        # by the always-on automation daemon (hyprutil/automation.py), not
        # here -- that way it keeps working even when the tray isn't running.
        # This loop only displays status and drives manual actions.
        # `active` is read purely for the status line; starting/stopping the
        # fan daemon lives in the settings app, since it needs a pkexec
        # password prompt that a tray menu is a poor place to trigger.
        try:
            s = core.read_status()
            active = power.service_active(core.SERVICE)
            override = core.read_override()

            self._status_ready.emit({
                "temp": s["temp"], "pwm": s["pwm"], "fan1": s["fan1"], "fan2": s["fan2"],
                "active": active, "override": override, "kbd": kbd.available(),
            })
        finally:
            self._refreshing = False

    def _apply_status(self, data):
        self._last_status = data
        self.setIcon(QIcon(make_icon(data["temp"])))

        self.kbd_menu.setEnabled(bool(data.get("kbd")))

        self._update_status_text()

    def _update_status_text(self):
        data = getattr(self, "_last_status", None)
        if data is None:
            return
        profile = self._current_profile
        override = data["override"]
        rpm = max(data["fan1"] or 0, data["fan2"] or 0)
        mode = f"override {override}" if override != "auto" else f"{core.PROFILE_LABELS.get(profile, profile)} curve"
        temp_str = f"{data['temp']:.1f}°C" if data["temp"] is not None else "?°C"
        status_text = (
            f"{temp_str} | PWM {data['pwm']} | {rpm} RPM | "
            f"daemon {'running' if data['active'] else 'stopped'} ({mode})"
        )
        self.status_action.setText(status_text)
        self.setToolTip(f"hypr-util\n{status_text}")


def _acquire_instance_lock():
    """Return a bound socket acting as a lock, or None if already running.

    Uses a Unix socket in /run/user/<uid>/ so stale files are wiped on logout.
    Probes first so a crashed-and-left-behind socket doesn't block restarts.
    """
    lock_path = f"/run/user/{os.getuid()}/hypr-util-tray.lock"
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(lock_path)
        probe.close()
        return None  # live instance is listening
    except OSError:
        probe.close()
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(lock_path)
    srv.listen(1)
    return srv


def main():
    lock = _acquire_instance_lock()
    if lock is None:
        print("hypr-util tray is already running", file=sys.stderr)
        sys.exit(0)

    if core.HP_HWMON is None or core.CPU_HWMON is None:
        print("Could not find hp or k10temp hwmon devices", file=sys.stderr)
        sys.exit(1)
    core.ensure_config_defaults()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    tray = FanTray(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

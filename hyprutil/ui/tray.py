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

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .. import fan as core
from .. import power
from .. import rgb

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


def make_palette_icon(palette, width=32, height=16):
    """A preset's colours as a small stripe swatch for its menu entry.

    The tray used to list four entries named "Preset 1".."Preset 4" with
    nothing to tell them apart, so picking one was guesswork until the
    keyboard changed. One stripe is a solid colour, several mean it cycles,
    and an empty outline means the effect animates its own colours and
    ignores the palette entirely.
    """
    palette = list(palette)
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 3, 3)
    if palette:
        painter.setClipPath(path)
        step = width / len(palette)
        for i, hexval in enumerate(palette):
            # +1 on the width so neighbouring stripes overlap by a subpixel
            # and antialiasing leaves no pale seam between them.
            painter.fillRect(QRectF(i * step, 0, step + 1, height), QColor(f"#{hexval}"))
        painter.setClipping(False)
    painter.setPen(QColor(0, 0, 0, 60))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    if not palette:
        painter.drawLine(3, height - 3, width - 3, 3)
    painter.end()
    return QIcon(pixmap)


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
        self.rgb_preset_actions = {}
        rgb_menu = QMenu("Keyboard RGB")
        for slot in rgb.PRESET_SLOTS:
            # Label and swatch are filled in by _apply_status, which refreshes
            # them every poll -- presets can be renamed or re-saved in the
            # settings app while this menu already exists.
            act = self._make_action(
                "", checkable=True, slot=lambda checked, s=slot: self.apply_rgb_preset(s)
            )
            rgb_menu.addAction(act)
            self.rgb_preset_actions[slot] = act
        self.menu.addMenu(rgb_menu)
        self._actions.append(rgb_menu)

        # Brightness is the one lighting setting worth reaching without
        # opening the settings window at all -- it is what you change when
        # the room gets dark, not something you sit down to configure.
        brightness_menu = QMenu("Brightness")
        for label, percent in (("Off", 0), ("25%", 25), ("50%", 50), ("75%", 75), ("Full", 100)):
            act = self._make_action(
                label, slot=lambda checked, pct=percent: self.set_brightness(pct)
            )
            brightness_menu.addAction(act)
        self.menu.addMenu(brightness_menu)
        self._actions.append(brightness_menu)

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

    def set_brightness(self, percent):
        threading.Thread(target=self._set_brightness_worker, args=(percent,), daemon=True).start()

    def _set_brightness_worker(self, percent):
        try:
            look = rgb.read_current()
            look["brightness"] = round(percent * rgb.BRIGHTNESS_MAX / 100)
            rgb.apply_current(look)
        except Exception:
            logger.exception("failed to set brightness to %d%%", percent)

    def apply_rgb_preset(self, slot):
        # rgb.apply_preset() paces several HID messages to the keyboard and
        # takes a couple of hundred milliseconds -- do it off the Qt main
        # thread so a menu click doesn't freeze the UI.
        threading.Thread(target=self._apply_rgb_preset_worker, args=(slot,), daemon=True).start()

    def _apply_rgb_preset_worker(self, slot):
        if rgb.ready():
            try:
                rgb.apply_preset(slot)
            except Exception:
                logger.exception("failed to apply RGB preset %r", slot)

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
        # Reacting to power-profile changes (display refresh rate, RGB flash)
        # is handled by the always-on automation daemon (hyprutil/automation.py),
        # not here -- that way it keeps working even when the tray isn't
        # running. This loop only displays status and drives manual actions.
        # `active` is read purely for the status line; starting/stopping the
        # fan daemon lives in the settings app, since it needs a pkexec
        # password prompt that a tray menu is a poor place to trigger.
        try:
            s = core.read_status()
            active = power.service_active(core.SERVICE)
            override = core.read_override()
            active_slot = rgb.active_preset()
            presets = {slot: rgb.read_preset(slot) for slot in rgb.PRESET_SLOTS}

            self._status_ready.emit({
                "temp": s["temp"], "pwm": s["pwm"], "fan1": s["fan1"], "fan2": s["fan2"],
                "active": active, "override": override, "active_slot": active_slot,
                "presets": presets,
            })
        finally:
            self._refreshing = False

    def _apply_status(self, data):
        self._last_status = data
        self.setIcon(QIcon(make_icon(data["temp"])))

        for slot, act in self.rgb_preset_actions.items():
            preset = data["presets"][slot]
            act.setText(preset["name"])
            # Blank swatch for effects that ignore colour, rather than
            # advertising colours the keyboard will not show.
            act.setIcon(make_palette_icon(rgb.look_colors(preset)))
            # Unchecked across the board when a hand-edited look is on the
            # keyboard, rather than leaving a tick on whichever preset was
            # applied last -- that tick used to outlive the colours it named.
            act.setChecked(slot == data["active_slot"])

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
    rgb.ensure_defaults()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    tray = FanTray(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

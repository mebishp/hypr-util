"""Tray app for the hypr-util custom fan curve daemon.

Quick status and actions only -- full curve editing, RGB configuration, and
log viewing live in the settings app (launched from here via "Open hypr-util...").
"""
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .. import display
from .. import fan as core
from .. import rgb

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ENTRY = REPO_ROOT / "app.py"


def make_icon(temp):
    if temp is None:
        color = QColor("gray")
    elif temp < 50:
        color = QColor("#4caf50")
    elif temp < 70:
        color = QColor("#ffb300")
    else:
        color = QColor("#e53935")

    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(QColor(0, 0, 0, 0))
    painter.drawEllipse(4, 4, 56, 56)
    painter.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPointSize(22)
    painter.setFont(font)
    label = str(int(temp)) if temp is not None else "?"
    painter.drawText(pixmap.rect(), 0x84, label)
    painter.end()
    return pixmap


class FanTray(QSystemTrayIcon):
    def __init__(self, app):
        super().__init__()
        self.app = app
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
            name = rgb.read_preset(slot).get("name", f"Preset {slot}")
            act = self._make_action(
                name, checkable=True, slot=lambda checked, s=slot: self.apply_rgb_preset(s)
            )
            rgb_menu.addAction(act)
            self.rgb_preset_actions[slot] = act
        self.menu.addMenu(rgb_menu)
        self._actions.append(rgb_menu)

        self.menu.addSeparator()
        self.toggle_action = self._make_action("...", slot=self.toggle_daemon)
        self.restart_action = self._make_action("Restart Daemon", slot=lambda: core.service_action("restart"))
        self.menu.addAction(self.toggle_action)
        self.menu.addAction(self.restart_action)

        self.menu.addSeparator()
        open_app_action = self._make_action("Open hypr-util...", slot=self.open_app)
        self.menu.addAction(open_app_action)

        self.menu.addSeparator()
        quit_action = self._make_action("Quit Tray", slot=app.quit)
        self.menu.addAction(quit_action)

        self.setContextMenu(self.menu)
        self.setVisible(True)

        self._last_profile = None

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

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
        subprocess.Popen([sys.executable, str(APP_ENTRY)])

    def toggle_daemon(self):
        core.service_action("stop" if core.service_active() else "start")

    def apply_rgb_preset(self, slot):
        if rgb.ready():
            try:
                rgb.apply_preset(slot)
            except Exception:
                pass

    def refresh(self):
        s = core.read_status()
        temp = s["temp"]
        active = core.service_active()
        override = core.read_override()
        profile = core.current_power_profile()
        rpm = max(s["fan1"] or 0, s["fan2"] or 0)
        pwm = s["pwm"]

        self.setIcon(QIcon(make_icon(temp)))

        if profile != self._last_profile:
            target_hz = core.PROFILE_REFRESH_HZ.get(profile)
            if target_hz is not None:
                display.set_refresh_rate(target_hz)
            # Skip the flash on the very first detection at startup (when
            # _last_profile is still None) -- that's a sync, not a change.
            if self._last_profile is not None and rgb.ready():
                threading.Thread(target=rgb.flash_for_profile, args=(profile,), daemon=True).start()
            self._last_profile = profile

        for p, act in self.profile_actions.items():
            act.setChecked(p == profile)

        active_slot = rgb.active_preset()
        for slot, act in self.rgb_preset_actions.items():
            act.setChecked(slot == active_slot)

        self.toggle_action.setText("Stop Daemon" if active else "Start Daemon")
        self.restart_action.setEnabled(active)

        mode = f"override {override}" if override != "auto" else f"{core.PROFILE_LABELS.get(profile, profile)} curve"
        temp_str = f"{temp:.1f}°C" if temp is not None else "?°C"
        status_text = f"{temp_str} | PWM {pwm} | {rpm} RPM | daemon {'running' if active else 'stopped'} ({mode})"
        self.status_action.setText(status_text)
        self.setToolTip(f"hypr-util\n{status_text}")


def main():
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

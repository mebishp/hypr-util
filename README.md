# hypr-util

<img src="system/icons/hicolor/scalable/apps/org.hyprnon.hyprutil-v2.svg" width="96" height="96" alt="hypr-util icon">

Fan curve and keyboard RGB control for this HP Omen laptop.

## Parts

- `bin/hyprutil`: unified launcher -- `hyprutil app|tray|daemon|flash` (settings window, tray icon, automation daemon, one-off RGB test)
- `hyprutil/`: shared backend (fan curve, RGB presets, display refresh rate) plus the GTK4/Adwaita settings window and PyQt6 tray icon under `hyprutil/ui/`
- `firefly-ctl/`: Rust CLI that talks to the keyboard's USB RGB controller
- `system/`: udev rule, systemd service, fan curve daemon script, desktop entries, D-Bus service file, icon

## Setup

```
./setup.sh
```

Installs missing packages, builds `firefly-ctl`, and installs the udev rule, systemd service, and desktop entries. Safe to re-run after pulling changes, it only touches what actually changed.

## Acknowledgement

https://github.com/Arjun31415/Firefly-cli

For the RGB logic (Implementing this cli)

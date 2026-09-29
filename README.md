<p align="center">
  <img src="system/icons/hicolor/scalable/apps/org.hyprnon.hyprutil.svg" width="120" height="120" alt="hypr-util icon">
</p>

<h1 align="center">hypr-util</h1>

<p align="center">Fan curve and keyboard RGB control for HP Omen laptops and the Cosmic Byte Firefly</p>

<p align="center">
  <img alt="platform" src="https://img.shields.io/badge/platform-Linux-informational">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="desktop" src="https://img.shields.io/badge/desktop-GNOME%20%7C%20KDE%20%7C%20Hyprland%20%7C%20any-success">
</p>

---

Two keyboards, two protocols: the laptop's own four-zone backlight through
the HP BIOS mailbox, and the external Firefly through its USB HID interface
-- and a multi-point fan curve that follows the power profile.

## ✨ What you get

- 🌈 **Two keyboards, one look** -- ten effects, presets, and status flashes
  on the laptop keyboard, with the option to make the external Firefly
  match it
- ⌨️ **Firefly RGB** -- spoken directly over its `hidraw` node, no helper
  binary, no libusb, no `setcap`
- 🌡️ **Fan curve** -- a multi-point curve per power profile, no root needed
  to view it
- 🖥️ **A settings window and a tray icon** -- GTK4/Adwaita and Qt6, works on
  any desktop (see [docs/desktop-environments.md](docs/desktop-environments.md))
- 🔌 **No always-on root daemon** for the laptop keyboard -- a small DKMS
  module exposes a few sysfs files instead

## 🚀 Quick start

```bash
./setup.sh
```

Installs into `/usr/local`, builds the `hyprkbd` DKMS module, and starts the
services. Re-run it after `git pull`.

```bash
hyprutil app                                # settings window
hyprutil kbd set --color ff0000 --zone wasd
hyprutil kbd match --enabled on --now       # make the Firefly follow it
```

Full command reference: [docs/usage.md](docs/usage.md).

## 📦 Parts

| | |
|---|---|
| `hyprutil/` | the Python package -- fan curve, RGB presets, display refresh rate, the settings window and tray, and the automation daemon |
| `hyprutil/rgb/` | the Firefly's HID protocol, spoken directly to its `hidraw` node |
| `hyprutil/kbd/` | the laptop's own four-zone keyboard: driver interface, lighting effects, status flashes, presets, and matching the Firefly to it |
| `kernel/hyprkbd/` | the DKMS kernel module that carries the laptop keyboard's colours, so nothing above it needs root |
| `bin/hyprutil` | run-from-checkout launcher, for working on the code without installing |
| `system/` | everything installed outside the repo (`systemd/`, `udev/`, `dbus/`, `desktop/`, `icons/`, `sleep/`) |

## 📚 Documentation

| | |
|---|---|
| [docs/installation.md](docs/installation.md) | Setup, uninstall, and building the kernel module by hand |
| [docs/usage.md](docs/usage.md) | The full `hyprutil` CLI and the systemd services |
| [docs/keyboard.md](docs/keyboard.md) | The `hyprkbd` driver, its WMI protocol, effects, status flashes, and matching the Firefly |
| [docs/firefly.md](docs/firefly.md) | The Firefly's own HID protocol |
| [docs/desktop-environments.md](docs/desktop-environments.md) | What works everywhere, and the one thing that's GNOME-only |

## 🖥️ Does this work outside GNOME?

Yes. Fan curve, both keyboards' lighting (including matching them to each
other), the settings window, and the tray icon all work identically on KDE
Plasma, Hyprland, Sway, XFCE, or anything else. The only GNOME-specific
piece is one side effect of the automation daemon (bumping the display's
refresh rate via Mutter's D-Bus API when you leave power-saver mode), and it
fails silently everywhere else -- nothing breaks, it just doesn't happen.
Details: [docs/desktop-environments.md](docs/desktop-environments.md).

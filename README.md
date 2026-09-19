# hypr-util

<img src="system/icons/hicolor/scalable/apps/org.hyprnon.hyprutil.svg" width="106" height="106" alt="hypr-util icon">

Fan curve and keyboard RGB control for HP Omen laptop and Cosmic Byte Firefly

## Parts

- `hyprutil/`: the Python package -- fan curve, RGB presets, display refresh rate, the GTK4/Adwaita settings window and PyQt6 tray under `hyprutil/ui/`, and the automation daemon
- `hyprutil/rgb/device.py`: the keyboard's HID protocol, spoken directly to its hidraw node -- no helper binary, no libusb, no setcap
- `bin/hyprutil`: run-from-checkout launcher, for working on the code without installing
- `system/`: everything installed outside the repo, in subdirectories named for where it goes (`bin/`, `dbus/`, `desktop/`, `icons/`, `sleep/`, `systemd/`, `udev/`)

## Setup

```
./setup.sh
```

Installs into `/usr/local` (override with `PREFIX=...`), then restarts the
services so what is running is what was just installed. Re-run it after
`git pull` -- the installed copy is what executes, so changes are not live
until you do.

```
./uninstall.sh
```

Removes everything setup.sh installed. Settings in `~/.config/hypr-util` are kept.

## Usage

```
hyprutil app       # settings window
hyprutil tray      # tray icon
hyprutil daemon    # automation daemon
hyprutil flash     # one-off RGB test
```

The tray and daemon normally run as systemd user services:

```
systemctl --user status hypr-util-tray hypr-util-daemon
journalctl --user -u hypr-util-tray -f
```

The fan curve runs as a system service:

```
systemctl status hypr-util-fancurve
```

## Keyboard protocol

The Cosmic Byte Firefly (USB `04d9:a1cd`) exposes a vendor HID collection on
interface 2: 8-byte Feature reports carry commands, a 64-byte Output report
carries bulk payloads, and a Feature read returns the reply. Bulk reads are
command, then ack, then read -- the ack is required or the device sends
nothing.

`0x08 SetLEDType` takes seven parameters, named by the vendor software as
`type, brightness, speed, direction, color, bl0, bl1`:

| byte | meaning | range |
|---|---|---|
| type | effect | 0-11 |
| brightness | | 0-63 |
| speed | 1 fastest, 7 slowest | 1-7 |
| direction | only used by wave and wave2 | 0 right, 1 left |
| color | palette slot, or 7 for the device's own rainbow | 0-7 |
| bl0/bl1 | device-owned; read with `0x88` and echoed back | |

Colour works one slot at a time: the keyboard holds seven colours and an
effect shows exactly one of them, or ignores them entirely on 7 (LOOP).
There is no "cycle through my colours" mode.

The keyboard also holds three lighting profiles of its own (`0x21`), which
the first three presets are bound to so their lighting survives with nothing
running.

Do not probe this device with unlisted opcodes -- `0x0F` is
SetISPBootLoader.

## Acknowledgement

https://github.com/Arjun31415/Firefly-cli

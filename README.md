# hypr-util

<img src="system/icons/hicolor/scalable/apps/org.hyprnon.hyprutil.svg" width="106" height="106" alt="hypr-util icon">

Fan curve and keyboard RGB control for HP Omen laptop and Cosmic Byte Firefly

Two keyboards, two protocols: the laptop's own four-zone backlight through
the HP BIOS mailbox, and the external Firefly through its USB HID interface.

## Parts

- `hyprutil/`: the Python package -- fan curve, RGB presets, display refresh rate, the GTK4/Adwaita settings window and PyQt6 tray under `hyprutil/ui/`, and the automation daemon
- `hyprutil/rgb/device.py`: the Firefly's HID protocol, spoken directly to its hidraw node -- no helper binary, no libusb, no setcap
- `hyprutil/kbd/`: the laptop's own four-zone keyboard -- the HP BIOS mailbox (`mailbox.py`), the lighting model (`zones.py`), and the root service that owns the hardware (`service.py`)
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
hyprutil flash     # one-off Firefly RGB test

hyprutil kbd status                          # the laptop keyboard's four zones
hyprutil kbd probe                           # why it is not working
hyprutil kbd set --color ff0000 --zone wasd
hyprutil kbd set --effect wave --speed 4
hyprutil kbd off
```

The tray and daemon normally run as systemd user services:

```
systemctl --user status hypr-util-tray hypr-util-daemon
journalctl --user -u hypr-util-tray -f
```

The fan curve and the laptop keyboard lighting run as system services:

```
systemctl status hypr-util-fancurve hypr-util-kbd
```

## Laptop keyboard: the BIOS mailbox

The built-in keyboard has no USB lighting interface of its own. Its colours
go through the same ACPI-WMI mailbox as HP's performance controls, under a
second command id, and the kernel's `hp-wmi` driver -- which speaks that
mailbox for fans and the platform profile -- has no lighting code at all.
There is no character device for the GUID either, so the only route from
userspace is `acpi_call`, which hands an arbitrary ACPI method call to the
firmware. That is why this half needs root and runs as a service.

The method is `\_SB.WMID.WMAA(0, method_id, buffer)` (the `PNP0C14` device
whose WMI GUID is `5FB7F034-2C63-45E9-BE91-3D44E2C707E4`, object id `AA`).
`method_id` selects the answer size, not the operation: 1 = 0 bytes, 2 = 4,
3 = 128, 4 = 1024, 5 = 4096 -- the same encoding `hp-wmi.c` uses. The buffer
is `u32 signature 0x55434553 ("SECU"), u32 command, u32 command type, u32
data size, data`, and the reply is `u32 signature, u32 return code, data`.

| Command / type | Payload | Meaning |
|---|---|---|
| `0x20008` / `0x2B` | out4 `[0]` | Keyboard type: 0 standard, 1 four zones with numpad, 2 four zones, 3 per-key, 4/5 one zone |
| `0x20009` / `0x01` | out128 `[0]` bit 0 | Lighting supported (weak signal; legacy boards only) |
| `0x20009` / `0x02` | out128 | Colour table. Zone i = bytes `25+3i .. 27+3i` (R, G, B); bytes 0-24 are left as read |
| `0x20009` / `0x03` | 128 bytes in, out4 | Write the colour table (read-modify-write) |
| `0x20009` / `0x04` | out128 `[0]` | Backlight byte: bit 7 = lit, low bits = level. A smaller read is refused |
| `0x20009` / `0x05` | `{byte,0,0,0}`, out4 | Set the backlight byte |

Measured here (board `8BCD`, OMEN 16-xd0xxx):

- `0x20008/0x2B` is not implemented on this board -- `AE_AML_OPERAND_VALUE`
  at every answer size, so the keyboard type falls back to "standard layout"
  and four zones, which is what both reference implementations assume. It is
  that one command and not the command space: `0x20008/0x26` answers
  normally, which also settles that a 4-byte answer size is fine.
- Replies come back truncated to 34 data bytes, and that is the transport,
  not the firmware: acpi_call prints `min(length, avail / 6)` bytes into a
  256-byte buffer, so 42 of the 136 survive. Zones 0-2 read back, WASD does
  not, and writes are unaffected because only the reply is text. A build
  with `BUFFER_SIZE` at 1024 or more returns the whole table.
- `0x20009/0x01` read `0x07` on one call and had bit 0 clear on an earlier
  one -- the accumulating byte others have reported. Nothing here depends on
  it; the colour table answering is the proof.

Zones are HP's numbering: 0 right, 1 middle, 2 left, 3 WASD. Brightness is
applied by scaling the colours before they are written, because the level
bits of the backlight byte do nothing on these boards. Effects are frames:
the firmware runs no animation of its own, so breathe, cycle and wave are
computed in `zones.py` and the colour table is rewritten about eight times a
second while one is running.

Keyboard type 3 (per-key) answers every one of these calls and drives
nothing: 128 bytes cannot address 176 LEDs, and those boards need their own
USB protocol, which nobody has published for the pre-2025 models. The page
says so rather than offering controls that cannot work.

Protocol details above were read out of the Ohman project's research notes
and lighting module, which document the vendor software's own behaviour.

## Firefly protocol

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

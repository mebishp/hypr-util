# hypr-util

<img src="system/icons/hicolor/scalable/apps/org.hyprnon.hyprutil.svg" width="106" height="106" alt="hypr-util icon">

Fan curve and keyboard backlight control for HP Omen laptops

The laptop's own four-zone keyboard backlight, driven through the HP BIOS
mailbox, and a multi-point fan curve that follows the power profile.

## Parts

- `hyprutil/`: the Python package -- fan curve, display refresh rate, the GTK4/Adwaita settings window and PyQt6 tray under `hyprutil/ui/`, and the automation daemon
- `hyprutil/kbd/`: the laptop's four-zone keyboard -- the driver's sysfs files (`device.py`), the lighting model and ten effects (`zones.py`), the animation service (`effects.py`), status lights (`indicators.py` over `sysinfo.py`), and saved looks (`presets.py`)
- `kernel/hyprkbd/`: the DKMS kernel module that carries the laptop keyboard's colours, so nothing above it needs root
- `bin/hyprutil`: run-from-checkout launcher, for working on the code without installing
- `system/`: everything installed outside the repo, in subdirectories named for where it goes (`bin/`, `dbus/`, `desktop/`, `icons/`, `modules-load/`, `sleep/`, `systemd/`, `udev/`)

## Setup

```
./setup.sh
```

Installs into `/usr/local` (override with `PREFIX=...`), then restarts the
services so what is running is what was just installed. Re-run it after
`git pull` -- the installed copy is what executes, so changes are not live
until you do.

It also builds the `hyprkbd` keyboard lighting module through DKMS, which
needs `dkms` and your kernel's headers. If either is missing it says so and
carries on: everything except the Laptop keyboard page works without it.

```
./uninstall.sh
```

Removes everything setup.sh installed. Settings in `~/.config/hypr-util` are kept.

## Usage

```
hyprutil app       # settings window
hyprutil tray      # tray icon
hyprutil daemon    # automation daemon

hyprutil kbd status                          # the keyboard's four zones
hyprutil kbd probe                           # why it is not working
hyprutil kbd set --color ff0000 --zone wasd
hyprutil kbd set --effect wave --speed 4
hyprutil kbd off
hyprutil kbd reload                          # put the saved look back

hyprutil kbd effects                         # the ten effects, and what each does
hyprutil kbd preset                          # list the four slots
hyprutil kbd preset 3                        # apply one
hyprutil kbd preset --save 3 --name Ember    # save the current look into one
hyprutil kbd indicators --battery on --low 20
hyprutil kbd saver --enabled on --brightness 30
```

None of these need root. `hyprutil kbd probe` is the one to run when
something is wrong -- it reports whether the driver is loaded, whether this
user may write to it, and what every attribute currently reads.

The tray and daemon normally run as systemd user services:

```
systemctl --user status hypr-util-tray hypr-util-daemon
journalctl --user -u hypr-util-tray -f
```

The fan curve runs as a system service, and so does the keyboard's
boot-and-resume restore -- a oneshot, not a daemon. The effect animation
runs in your own session:

```
systemctl status hypr-util-fancurve hypr-util-kbd
systemctl --user status hypr-util-kbd-effects
```

## Laptop keyboard: the hyprkbd driver

The built-in keyboard has no USB lighting interface of its own. Its colours
go through the same ACPI-WMI mailbox as HP's performance controls, under a
second command id, and the kernel's `hp-wmi` driver -- which speaks that
mailbox for fans and the platform profile -- has no lighting code at all.
There is no character device for the GUID either.

`kernel/hyprkbd/` is the missing piece: a small DKMS module that makes the
call in the kernel and publishes the result as ordinary files. It claims no
WMI GUID, so `hp-wmi` keeps everything it already handles.

```
/sys/devices/platform/hyprkbd/
    keyboard_type  type_name  zones  kind  supported
    colors         zone0 .. zone3    backlight  backlight_raw  refresh
```

A udev rule hands the writable ones to the user who ran `setup.sh`, so
changing a colour is a `write()` by an ordinary process:

```
echo 'ff0000 00ff00 0000ff ffffff' > /sys/devices/platform/hyprkbd/colors
echo 1 > /sys/devices/platform/hyprkbd/backlight
```

`colors` sets every zone in one WMI call, which is what an effect frame
wants; writing `zone0`..`zone3` one at a time would cost four. The driver
holds the firmware's 128-byte table, so a write is a write -- no read first.

This replaced a root daemon reaching the firmware through `acpi_call` over a
Unix socket. `acpi_call` would have given the desktop session every ACPI
method on the machine, and it formatted replies as text into a 256-byte
buffer, so the 136-byte answer came back cut to 42 and the WASD zone could
be written but never read. Both problems are gone: the module exposes these
calls and no others, and the table reads back whole.

### The protocol the module speaks

The method is `\_SB.WMID.WMAA(0, method_id, buffer)` (the `PNP0C14` device
whose WMI GUID is `5FB7F034-2C63-45E9-BE91-3D44E2C707E4`, object id `AA`).
`method_id` selects the answer size, not the operation: 1 = 0 bytes, 2 = 4,
3 = 128, 4 = 1024, 5 = 4096 -- the same encoding `hp-wmi.c` uses. The buffer
is `u32 signature 0x55434553 ("SECU"), u32 command, u32 command type, u32
data size, data`, and the reply is `u32 signature, u32 return code, data`.
The data block is padded to 128 bytes however little of it means anything;
this firmware insists on it.

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
- `0x20009/0x01` read `0x07` on one call and had bit 0 clear on an earlier
  one -- the accumulating byte others have reported. Nothing here depends on
  it; the colour table answering is the proof, in both directions.

Zones are HP's numbering: 0 right, 1 middle, 2 left, 3 WASD. Brightness is
applied by scaling the colours before they are written, because the level
bits of the backlight byte do nothing on these boards. Effects are frames:
the firmware runs no animation of its own, so breathe, cycle and wave are
computed in `zones.py` and the colour table is rewritten about eight times a
second while one is running.

Keyboard type 3 (per-key) answers every one of these calls and drives
nothing: 128 bytes cannot address 176 LEDs, and those boards need their own
USB protocol, which nobody has published for the pre-2025 models. The driver
reports `kind = perkey` and the page says so, rather than offering controls
that cannot work.

Protocol details above were read out of the Ohman project's research notes
and lighting module, which document the vendor software's own behaviour.

### Effects

Ten, all computed here and written as frames -- the firmware runs no
animation of its own. `hyprutil kbd effects` prints this list with a line
each.

| | |
|---|---|
| `static` | Each zone holds the colour you picked. |
| `gradient` | A smooth blend from the left zone's colour to the right zone's. |
| `breathe` | Your colours fading gently in and out together. |
| `pulse` | A sharp flash with a long dark gap between beats. |
| `cycle` | The whole board through the spectrum, in step. |
| `wave` | The spectrum, offset across the zones so it travels. |
| `sweep` | A bright band of your colour running left to right. |
| `aurora` | Slow, drifting hues that never quite repeat. |
| `fire` | A warm flicker, each zone burning on its own. |
| `meter` | Fills left to right with CPU load, green through red. |

The travelling ones need to know where a zone physically sits, which the
firmware's numbering does not say -- zone 0 is the right-hand band, zone 2
the left. `ZONE_POSITION` in `zones.py` supplies that, with WASD at 0.22
rather than a quarter of the way across, because it sits over the keys it is
named for rather than filling a band.

`gradient` and `static` are a single write and then nothing, so the effects
service idles through them rather than waking eight times a second.

### Indicators

A zone can be held aside to show what the machine is doing.

| | |
|---|---|
| Power profile | green saver, yellow balanced, red performance |
| Battery low | amber, solid |
| Battery critical | red, blinking |
| Charging | green, pulsing |

They are composited over the look rather than replacing it, and the higher
priority wins a contested zone outright -- a blend of "battery critical" and
"performance mode" is a colour that means neither.

**They keep working with the lighting switched off.** That is the point of
the design: the backlight goes on, every other zone is written `000000`, and
the board reads as dark with one status light. Measured on this hardware, a
zone set to black is genuinely dark rather than dimly lit, which is what
makes that honest. With indicators off and the look off, the backlight byte
goes to 0 and the keyboard is properly dark.

### Battery saver

Four lit zones are a real draw. With the saver on, running on battery caps
the brightness and can stop animations -- the cost of an effect is the eight
WMI calls a second, not the brightness of any one frame. The saved look is
never touched, so plugging back in returns exactly what was chosen.

An unknown power source (a desktop, a VM, no mains adapter in `/sys`) counts
as mains. Dimming someone's keyboard because we could not find out whether
they were on battery would be the wrong way to be wrong.

### Presets

Four named slots holding a whole look -- effect, four colours, brightness and
speed. Applying one writes `current.json` like any other change, so the
effects service picks it up with no extra plumbing. They live in
`~/.config/hypr-util/kbd/presets/`, and the settings in `settings.json`
beside them; the look itself stays in `current.json`, which is the only one
of the three that changes often enough to be worth watching in a loop.

### Building it by hand

`setup.sh` does this for you, but the module is a normal out-of-tree build:

```
cd kernel/hyprkbd && make && sudo insmod hyprkbd.ko
```

It needs your kernel's headers, and it follows whatever compiler built the
running kernel -- a Clang-built kernel (CachyOS ships one) rejects a
GCC-built module.


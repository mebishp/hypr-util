# Laptop keyboard: the hyprkbd driver

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

## The protocol the module speaks

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

## Effects

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

## Status flashes

When the machine changes, the **whole keyboard** takes a colour for a couple
of seconds and then the effect you were running comes back exactly where it
left off.

| | |
|---|---|
| Power profile changed | green saver, yellow balanced, red performance; pulsing |
| Battery fell below low | amber, solid |
| Battery fell below critical | red, blinking, repeating every five minutes |
| Charger in | green, pulsing |
| Charger out | amber, pulsing |

This replaced an earlier design that held one zone aside for the whole
session. A permanently coloured corner is a poor status light: nothing about
it moves when the status does, so you stop seeing it; it costs a quarter of
the board all the time; and it argues with whatever effect is running over
the other three zones. A flash is the opposite of all three.

They fire on **changes, not states**. "The battery is at 9%" is not news
eight times a second; "the battery has just fallen below 10%" is. A critical
battery is the one exception and repeats on a timer.

The look is not disturbed. Its phase is held while the flash is up, so a
sweep resumes mid-travel rather than restarting, and the two fading edges of
the flash crossfade against the look rather than dipping through black.

**They work with the lighting switched off.** The backlight comes on for the
flash and goes back off after it, so a keyboard someone has deliberately left
dark can still say something and then be dark again. With flashes off and the
look off, the backlight byte goes to 0 and the keyboard is properly dark.

## Three zones or four

The keyboard has four zones, but the WASD cluster sits *inside* the left
third rather than beside it. For the effects that run as one picture across
the board -- gradient, wave, sweep, aurora, the CPU meter -- WASD takes the
left zone's colour and the keyboard reads as three clean bands. For the rest
-- static, breathe, pulse, fire -- a separately lit cluster is the point, so
it keeps its own colour. The settings app greys out the WASD picker and says
why when the current effect folds it in.

## Battery saver

Four lit zones are a real draw. With the saver on, running on battery caps
the brightness and can stop animations -- the cost of an effect is the eight
WMI calls a second, not the brightness of any one frame. The saved look is
never touched, so plugging back in returns exactly what was chosen.

An unknown power source (a desktop, a VM, no mains adapter in `/sys`) counts
as mains. Dimming someone's keyboard because we could not find out whether
they were on battery would be the wrong way to be wrong.

## Presets

Four named slots holding a whole look -- effect, four colours, brightness and
speed. Applying one writes `current.json` like any other change, so the
effects service picks it up with no extra plumbing. They live in
`~/.config/hypr-util/kbd/presets/`, and the settings in `settings.json`
beside them; the look itself stays in `current.json`, which is the only one
of the three that changes often enough to be worth watching in a loop.

See [Installation](installation.md#building-the-kernel-module-by-hand) for
building `hyprkbd.ko` by hand.

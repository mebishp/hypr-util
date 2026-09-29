# Firefly protocol

The Cosmic Byte Firefly (USB `04d9:a1cd`) exposes a vendor HID collection on
interface 2: 8-byte Feature reports carry commands, a 64-byte Output report
carries bulk payloads, and a Feature read returns the reply. Bulk reads are
command, then ack, then read -- the ack is required or the device sends
nothing.

`hyprutil/rgb/device.py` speaks to it directly over its `hidraw` node --
no helper binary, no libusb, no `setcap`.

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

Presence is detected event-driven, via udev netlink uevents (`hyprutil/rgb/watch.py`)
rather than polling, so there's no recurring check and no wasted work while
the device is unplugged.

See [Matching the two keyboards](keyboard.md#matching-the-two-keyboards) for
how this keyboard's lighting is kept in step with the laptop's.

## Acknowledgement

https://github.com/Arjun31415/Firefly-cli

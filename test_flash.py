#!/usr/bin/env python3
"""Utility for tuning the power-profile-change flash notification.

Applies an effect/color for a duration, then reverts to whatever preset
was last active (set one first via the tray or app if nothing reverts).

Usage:
    python3 test_flash.py <effect> <hexcolor> <duration_seconds> [color_idx]
    python3 test_flash.py --list-effects
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hyprutil.rgb import controller, notify, presets


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--list-effects":
        print("\n".join(controller.EFFECTS))
        return

    if len(sys.argv) < 4:
        print(__doc__)
        print(f"available effects: {', '.join(controller.EFFECTS)}")
        sys.exit(1)

    effect = sys.argv[1]
    color = sys.argv[2]
    duration = float(sys.argv[3])
    color_idx = int(sys.argv[4]) if len(sys.argv) > 4 else 7

    slot = presets.active_preset()
    print(f"current active preset: {presets.read_preset(slot)['name'] if slot else '(none)'}")
    print(f"flashing effect={effect!r} color=#{color} color_idx={color_idx} for {duration}s...")
    notify.flash(effect, color, duration, color_idx)
    print("reverted")


if __name__ == "__main__":
    main()

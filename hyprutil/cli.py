"""hyprutil: unified command-line entry point.

Dispatches to the settings app, tray icon, automation daemon, or the
keyboard lighting service.
"""
import argparse
import json
import sys


def _run_tray(args):
    from .ui.tray import main
    main()


def _run_daemon(args):
    from .automation import main
    main()


def _run_kbd_effects(args):
    from .kbd.effects import main
    main()


def _kbd_zone_indices(name):
    from . import kbd

    names = {kbd.ZONE_NAMES[z].lower(): z for z in kbd.DISPLAY_ORDER}
    if name == "all":
        return list(names.values())
    if name not in names:
        raise SystemExit(f"unknown zone {name!r}; pick one of all, {', '.join(names)}")
    return [names[name]]


def _kbd_bool(value):
    """--flag on|off, which reads better than a pair of --x/--no-x flags
    once there are six of them."""
    return value == "on"


def _run_kbd(args):
    """The laptop's own four-zone keyboard. See hyprutil/kbd/ for the protocol."""
    from . import kbd

    try:
        if args.kbd_command == "probe":
            print(json.dumps(kbd.probe(), indent=2))
            return
        if args.kbd_command == "effects":
            for effect in kbd.EFFECTS:
                marks = []
                if effect in kbd.ANIMATED:
                    marks.append("animated")
                if effect in kbd.COLOURLESS_EFFECTS:
                    marks.append("ignores your colours")
                if kbd.merges_wasd(effect):
                    marks.append("WASD runs with the left zone")
                suffix = f"  [{', '.join(marks)}]" if marks else ""
                print(f"{effect:<9} {kbd.EFFECT_DESCRIPTIONS[effect]}{suffix}")
            return
        if args.kbd_command == "reload":
            kbd.reload()
            print("applied the saved look")
            return
        if args.kbd_command == "status":
            _print_kbd_status(kbd)
            return
        if args.kbd_command == "preset":
            _run_kbd_preset(kbd, args)
            return
        if args.kbd_command == "indicators":
            _run_kbd_indicators(kbd, args)
            return
        if args.kbd_command == "saver":
            _run_kbd_saver(kbd, args)
            return

        look = dict(kbd.read_current())
        if args.kbd_command == "on":
            look["on"] = True
        elif args.kbd_command == "off":
            look["on"] = False
        else:  # set
            if args.color:
                color = kbd.to_hex(args.color.lstrip("#"))
                colors = list(look["colors"])
                for zone in _kbd_zone_indices(args.zone):
                    colors[zone] = color
                look["colors"] = colors
                look["on"] = True
            if args.effect:
                look["effect"] = args.effect
            if args.speed is not None:
                look["speed"] = args.speed
            if args.brightness is not None:
                look["brightness"] = args.brightness
        kbd.apply(look)
        print("applied")
    except kbd.DeviceError as e:
        raise SystemExit(str(e))


def _print_kbd_status(kbd):
    reply = kbd.status()
    keyboard = reply.get("keyboard")
    print(f"keyboard:   {keyboard['describe'] if keyboard else 'none'}"
          + (f" (type {keyboard['type']}, {keyboard['type_name']})" if keyboard else ""))
    print(f"backlight:  {'on' if reply.get('lit') else 'off'}")
    look = reply.get("look") or {}
    effective = reply.get("effective_look") or look
    print(f"effect:     {look.get('effect')} at speed {look.get('speed')}")
    print(f"brightness: {look.get('brightness')}")
    if reply.get("saving"):
        # Worth saying out loud: the keyboard is deliberately not showing
        # what the saved look says, and someone who did not set this up
        # would otherwise be hunting a bug.
        print(f"            battery saver is on -- showing "
              f"{effective.get('effect')} at brightness {effective.get('brightness')}")
    for zone in kbd.DISPLAY_ORDER:
        colors = look.get("colors") or []
        if zone < len(colors):
            print(f"  {kbd.ZONE_NAMES[zone]:<7} #{colors[zone]}")

    telemetry = reply.get("telemetry") or {}
    battery = telemetry.get("battery")
    power = telemetry.get("profile") or "unknown"
    if battery is not None:
        charge = " charging" if telemetry.get("charging") else ""
        power += f", battery {battery}%{charge}"
    print(f"machine:    {power}")

    alert = reply.get("alert")
    if alert:
        print(f"alert:      {alert['label']} -- the whole board #{alert['color']}"
              f" ({alert['style']}), {alert['remaining']}s left")
    else:
        print("alert:      none (the board is showing the look)")
    if reply.get("error"):
        print(f"error:      {reply['error']}")


def _run_kbd_preset(kbd, args):
    if args.list or (args.slot is None and not args.save):
        current = kbd.read_current()
        for slot, look in kbd.presets.read_all().items():
            colors = " ".join(look["colors"])
            marker = "*" if _looks_match(look, current) else " "
            print(f"{marker}{slot}  {look['name']:<12} {look['effect']:<9} {colors}")
        return
    if args.save:
        saved = kbd.save_preset(args.save, name=args.name)
        print(f"saved the current look into slot {args.save} as {saved['name']!r}")
        return
    applied = kbd.apply_preset(args.slot)
    name = kbd.presets.read(args.slot)["name"]
    print(f"applied preset {args.slot} ({name})")
    return applied


def _looks_match(preset, current):
    """Whether a preset is what the keyboard is currently set to.

    Compared on the look only -- the name is not part of what is showing.
    """
    keys = ("effect", "colors", "brightness", "speed")
    return all(preset.get(k) == current.get(k) for k in keys)


def _run_kbd_indicators(kbd, args):
    patch = {"indicators": {}}
    section = patch["indicators"]
    if args.enabled:
        section["enabled"] = _kbd_bool(args.enabled)
    if args.when_off:
        section["when_off"] = _kbd_bool(args.when_off)
    if args.duration is not None:
        section["duration"] = args.duration
    if args.profile:
        section.setdefault("profile", {})["enabled"] = _kbd_bool(args.profile)
    if args.battery:
        section.setdefault("battery", {})["enabled"] = _kbd_bool(args.battery)
    if args.charging:
        section.setdefault("battery", {})["show_charging"] = _kbd_bool(args.charging)
    if args.low is not None:
        section.setdefault("battery", {})["low"] = args.low
    if args.critical is not None:
        section.setdefault("battery", {})["critical"] = args.critical
    if args.repeat is not None:
        section.setdefault("battery", {})["repeat_critical"] = args.repeat

    if section:
        kbd.update_settings(patch)
    config = kbd.read_settings()["indicators"]
    print(f"indicators:   {'on' if config['enabled'] else 'off'}")
    print(f"when off:     {'shown' if config['when_off'] else 'hidden'}")
    print(f"flash:        the whole board for {config['duration']:g}s,"
          " then back to the look")
    print(f"power profile {'on' if config['profile']['enabled'] else 'off'}")
    battery = config["battery"]
    print(f"battery       {'on' if battery['enabled'] else 'off'},"
          f" low at {battery['low']}%, critical at {battery['critical']}%"
          f" (repeating every {battery['repeat_critical']:g}s)")
    print(f"charger       {'on' if battery['show_charging'] else 'off'}")
    alert = kbd.status().get("alert")
    print("showing now:  " + (alert["label"] if alert else "nothing"))


def _run_kbd_saver(kbd, args):
    patch = {}
    if args.enabled:
        patch["enabled"] = _kbd_bool(args.enabled)
    if args.brightness is not None:
        patch["brightness"] = args.brightness
    if args.static_only:
        patch["static_only"] = _kbd_bool(args.static_only)
    if patch:
        kbd.update_settings({"battery_saver": patch})
    config = kbd.read_settings()["battery_saver"]
    telemetry = kbd.sysinfo.telemetry()
    where = "battery" if telemetry.get("on_ac") is False else "mains"
    print(f"battery saver: {'on' if config['enabled'] else 'off'}")
    print(f"  brightness:  caps at {config['brightness']}")
    print(f"  animations:  {'stopped' if config['static_only'] else 'kept'} on battery")
    print(f"  right now:   on {where}"
          + (" -- saver active" if config["enabled"] and where == "battery" else ""))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)

    # Handled before argparse: GApplication's own flags (e.g.
    # --gapplication-service, used by the D-Bus service file) start with
    # "--" and argparse.REMAINDER does not reliably pass those through a
    # subparser positional, so hand them off directly instead.
    if argv and argv[0] == "app":
        from .ui.app import main as app_main
        app_main(argv=[sys.argv[0]] + argv[1:])
        return

    parser = argparse.ArgumentParser(prog="hyprutil", description="Fan curve and keyboard RGB control.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("app", help="Open the settings window (GTK4/Adwaita)")
    sub.add_parser("tray", help="Run the tray icon (PyQt6)").set_defaults(func=_run_tray)
    sub.add_parser("daemon", help="Run the automation daemon").set_defaults(func=_run_daemon)

    # Imported here rather than at module scope so `hyprutil --help` does not
    # pay for it: the effect list has to be the real one, or --effect would
    # reject the six animations added after this parser was first written.
    from .kbd.zones import EFFECTS as KBD_EFFECTS

    kbd_parser = sub.add_parser("kbd", help="Laptop keyboard lighting (four zones)")
    kbd_sub = kbd_parser.add_subparsers(dest="kbd_command", required=True)
    kbd_sub.add_parser("status", help="What the keyboard is showing")
    kbd_sub.add_parser("probe", help="Diagnostics: the driver, keyboard type, raw values")
    kbd_sub.add_parser("on", help="Turn the backlight on")
    kbd_sub.add_parser("off", help="Turn the backlight off")
    kbd_set = kbd_sub.add_parser("set", help="Change colour, effect, speed or brightness")
    kbd_set.add_argument("--color", help="hex colour, e.g. ff0000")
    kbd_set.add_argument("--zone", default="all", help="all, left, middle, right or wasd")
    kbd_set.add_argument("--effect", choices=KBD_EFFECTS)
    kbd_set.add_argument("--speed", type=int, choices=range(1, 6))
    kbd_set.add_argument("--brightness", type=int, metavar="0-100")

    kbd_sub.add_parser("effects", help="List the effects, with what each one does")

    kbd_preset = kbd_sub.add_parser("preset", help="Apply, save or list the four presets")
    kbd_preset.add_argument("slot", nargs="?", type=int, choices=[1, 2, 3, 4],
                            help="the slot to apply; omit to list them")
    kbd_preset.add_argument("--save", type=int, choices=[1, 2, 3, 4],
                            metavar="SLOT", help="save the current look into this slot")
    kbd_preset.add_argument("--name", help="name to save it under")
    kbd_preset.add_argument("--list", action="store_true", help="list the presets")

    kbd_ind = kbd_sub.add_parser(
        "indicators",
        help="Flash the whole board on a profile or battery change",
    )
    kbd_ind.add_argument("--enabled", choices=["on", "off"])
    kbd_ind.add_argument("--when-off", choices=["on", "off"], dest="when_off",
                         help="flash even with the lighting switched off")
    kbd_ind.add_argument("--duration", type=float, metavar="SECONDS",
                         help="how long the board is held before the look comes back")
    kbd_ind.add_argument("--profile", choices=["on", "off"])
    kbd_ind.add_argument("--battery", choices=["on", "off"])
    kbd_ind.add_argument("--charging", choices=["on", "off"],
                         help="flash when the charger goes in or out")
    kbd_ind.add_argument("--low", type=int, metavar="PCT", help="low battery threshold")
    kbd_ind.add_argument("--critical", type=int, metavar="PCT")
    kbd_ind.add_argument("--repeat", type=float, metavar="SECONDS",
                         help="how often a critical battery says so again")

    kbd_saver = kbd_sub.add_parser("saver", help="Dim the keyboard on battery")
    kbd_saver.add_argument("--enabled", choices=["on", "off"])
    kbd_saver.add_argument("--brightness", type=int, metavar="0-100")
    kbd_saver.add_argument("--static-only", choices=["on", "off"], dest="static_only",
                           help="stop animations while on battery")

    kbd_parser.set_defaults(func=_run_kbd)

    kbd_sub.add_parser(
        "reload", help="Re-apply the saved look (what boot and resume run)"
    )

    sub.add_parser(
        "kbd-effects", help="Run the keyboard effect animation (systemd starts this)"
    ).set_defaults(func=_run_kbd_effects)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

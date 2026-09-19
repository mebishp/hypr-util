"""hyprutil: unified command-line entry point.

Dispatches to the settings app, tray icon, automation daemon, the laptop
keyboard lighting service, or a one-off RGB flash test.
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


def _run_flash(args):
    from .rgb import controller, notify, presets

    if args.list_effects:
        print("\n".join(controller.EFFECTS))
        return

    if args.effect is None or args.color is None or args.duration is None:
        print(
            "usage: hyprutil flash <effect> <hexcolor> <duration_seconds> [color_idx]",
            file=sys.stderr,
        )
        print(f"available effects: {', '.join(controller.EFFECTS)}", file=sys.stderr)
        sys.exit(1)

    slot = presets.active_preset()
    print(f"current active preset: {presets.read_preset(slot)['name'] if slot else '(none)'}")
    print(
        f"flashing effect={args.effect!r} color=#{args.color} "
        f"color_idx={args.color_idx} for {args.duration}s..."
    )
    notify.flash(args.effect, args.color, args.duration, args.color_idx)
    print("reverted")


def _run_kbd_daemon(args):
    from .kbd.service import main
    main()


def _kbd_zone_indices(name):
    from . import kbd

    names = {kbd.ZONE_NAMES[z].lower(): z for z in kbd.DISPLAY_ORDER}
    if name == "all":
        return list(names.values())
    if name not in names:
        raise SystemExit(f"unknown zone {name!r}; pick one of all, {', '.join(names)}")
    return [names[name]]


def _run_kbd(args):
    """The laptop's own four-zone keyboard. See hyprutil/kbd/ for the protocol."""
    from . import kbd

    try:
        if args.kbd_command == "probe":
            print(json.dumps(kbd.probe(), indent=2))
            return
        if args.kbd_command == "status":
            reply = kbd.status()
            keyboard = reply.get("keyboard")
            print(f"keyboard:   {keyboard['describe'] if keyboard else 'none'}"
                  + (f" (type {keyboard['type']}, {keyboard['type_name']})" if keyboard else ""))
            print(f"backlight:  {'on' if reply.get('lit') else 'off'}")
            look = reply.get("look") or {}
            print(f"effect:     {look.get('effect')} at speed {look.get('speed')}")
            print(f"brightness: {look.get('brightness')}")
            for zone in kbd.DISPLAY_ORDER:
                colors = look.get("colors") or []
                if zone < len(colors):
                    print(f"  {kbd.ZONE_NAMES[zone]:<7} #{colors[zone]}")
            if reply.get("error"):
                print(f"error:      {reply['error']}")
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
    except kbd.ServiceUnavailable as e:
        raise SystemExit(
            f"{e}\n"
            "The lighting service is not running. Start it with\n"
            "    sudo systemctl start hypr-util-kbd.service\n"
            "or run this command under sudo to drive the keyboard directly."
        )
    except kbd.MailboxError as e:
        raise SystemExit(str(e))


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

    kbd_parser = sub.add_parser("kbd", help="Laptop keyboard lighting (four zones)")
    kbd_sub = kbd_parser.add_subparsers(dest="kbd_command", required=True)
    kbd_sub.add_parser("status", help="What the keyboard is showing")
    kbd_sub.add_parser("probe", help="Diagnostics: transport, keyboard type, raw values")
    kbd_sub.add_parser("on", help="Turn the backlight on")
    kbd_sub.add_parser("off", help="Turn the backlight off")
    kbd_set = kbd_sub.add_parser("set", help="Change colour, effect, speed or brightness")
    kbd_set.add_argument("--color", help="hex colour, e.g. ff0000")
    kbd_set.add_argument("--zone", default="all", help="all, left, middle, right or wasd")
    kbd_set.add_argument("--effect", choices=["static", "breathe", "cycle", "wave"])
    kbd_set.add_argument("--speed", type=int, choices=range(1, 6))
    kbd_set.add_argument("--brightness", type=int, metavar="0-100")
    kbd_parser.set_defaults(func=_run_kbd)

    sub.add_parser(
        "kbd-daemon", help="Run the root keyboard lighting service (systemd starts this)"
    ).set_defaults(func=_run_kbd_daemon)

    flash_parser = sub.add_parser("flash", help="Manually apply an RGB effect/color, then revert")
    flash_parser.add_argument("effect", nargs="?")
    flash_parser.add_argument("color", nargs="?", help="hex color, e.g. ff0000")
    flash_parser.add_argument("duration", nargs="?", type=float)
    flash_parser.add_argument("color_idx", nargs="?", type=int, default=7)
    flash_parser.add_argument("--list-effects", action="store_true")
    flash_parser.set_defaults(func=_run_flash)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

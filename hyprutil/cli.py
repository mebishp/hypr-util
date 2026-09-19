"""hyprutil: unified command-line entry point.

Dispatches to the settings app, tray icon, automation daemon, or a one-off
RGB flash test.
"""
import argparse
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

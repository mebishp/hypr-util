"""hyprutil: unified command-line entry point.

Dispatches to the settings app, tray icon, automation daemon, a one-off RGB
flash test, focus mode, or the local todo list -- for scripting/keybinds
without going through a UI (the app/tray call the same focus.py/tasks.py
functions these subcommands do).
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


def _run_focus(args):
    from . import focus

    if args.action == "status":
        print(json.dumps(focus.read_state(), indent=2))
        return

    state = focus.read_state()
    if args.action == "on":
        new_active = True
    elif args.action == "off":
        new_active = False
    else:  # toggle
        new_active = not state["active"]

    try:
        focus.request(
            new_active,
            profile=args.profile,
            duration_s=args.duration * 60 if args.duration else None,
            hard_lock=args.lock,
        )
    except focus.HardLockError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print("focus mode " + ("on" if new_active else "off"))


def _run_task(args):
    from . import tasks

    if args.task_command == "add":
        task = tasks.add_task(tasks.DEFAULT_LIST_ID, args.title)
        print(f"added: {task['title']}  ({task['id']})")
    elif args.task_command == "list":
        items = tasks.tasks_for_list(tasks.DEFAULT_LIST_ID)
        if not items:
            print("(no tasks)")
        for t in items:
            mark = "x" if t["status"] == "completed" else " "
            print(f"[{mark}] {t['title']}  ({t['id']})")
    elif args.task_command == "done":
        if tasks.set_done(args.task_id, True) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            sys.exit(1)
        print("done")


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

    focus_parser = sub.add_parser("focus", help="Toggle/inspect focus mode")
    focus_parser.add_argument("action", choices=["on", "off", "toggle", "status"])
    focus_parser.add_argument("--profile", help="focus profile name to use (default: last-used)")
    focus_parser.add_argument("--duration", type=float, help="minutes; omit for indefinite")
    focus_parser.add_argument("--lock", action="store_true", help="hard-lock until --duration elapses")
    focus_parser.set_defaults(func=_run_focus)

    task_parser = sub.add_parser("task", help="Manage the local todo list")
    task_sub = task_parser.add_subparsers(dest="task_command", required=True)
    task_add = task_sub.add_parser("add", help="Add a task")
    task_add.add_argument("title")
    task_sub.add_parser("list", help="List tasks")
    task_done = task_sub.add_parser("done", help="Mark a task complete")
    task_done.add_argument("task_id")
    task_parser.set_defaults(func=_run_task)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

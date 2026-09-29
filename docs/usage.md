# Usage

```
hyprutil app       # settings window
hyprutil tray      # tray icon
hyprutil daemon    # automation daemon

hyprutil kbd status                          # the laptop keyboard's four zones
hyprutil kbd probe                           # why it is not working
hyprutil kbd set --color ff0000 --zone wasd
hyprutil kbd set --effect wave --speed 4
hyprutil kbd off
hyprutil kbd reload                          # put the saved look back

hyprutil kbd effects                         # the ten effects, and what each does
hyprutil kbd preset                          # list the four slots
hyprutil kbd preset 3                        # apply one
hyprutil kbd preset --save 3 --name Ember    # save the current look into one
hyprutil kbd indicators --battery on --low 20 --duration 3
hyprutil kbd saver --enabled on --brightness 30
```

None of these need root. `hyprutil kbd probe` is the one to run when
something is wrong -- it reports whether the driver is loaded, whether this
user may write to it, and what every attribute currently reads.

## Services

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

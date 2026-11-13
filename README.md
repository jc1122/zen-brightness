# zen-brightness

A GNOME brightness-key controller with separate HDR software dimming and
hardware PWM stages. One Python file; no permanent service or notification cache.

**v0.1.0 is a prerelease.** Software behavior is tested; physical brightness,
flicker and live notification positioning still need hardware acceptance.

## Behavior

- **SDR:** adjust hardware brightness without a software-dimming stage.
- **HDR:** Down reduces software SDR intensity to its minimum, then PWM. Up
  restores PWM first, then raises SDR intensity.
- Concurrent keypresses run sequentially in one short-lived `Gio.Application`.
- One notification is updated and closes 500 ms after the last popup update.
- D-Bus calls have two-second timeouts. Hardware and software values are checked
  after writes. A popup failure does not fail a successful brightness change.
- Notification-server restarts and manual dismissal discard obsolete IDs.

The script controls only the built-in panel. It does not toggle HDR or change
monitor layouts. Percentages describe the named stage, not measured panel
luminance. Errors go to stderr and, when launched by GNOME, its user journal.

## Supported target

Development target: ASUS Zenbook Flip UP3404VA, Ubuntu 26.04, GNOME/Mutter 50.1.
The constants `PANEL = "eDP-1"`, `DEVICE = "intel_backlight"` and `HW_FLOOR = 4`
are specific to that target. Hardware maximum and software ranges are read live.
Do not use these constants on another machine without hardware qualification.

Mutter DisplayConfig is GNOME-specific. An unknown color mode, unavailable panel
or failed write returns a nonzero status. Compatibility with future GNOME releases
is not assumed; see [maintenance](#maintenance).

## Install and bind keys

Run as the logged-in GNOME desktop user, without running the script as root.
On Ubuntu, install runtime dependencies:

```sh
sudo apt install python3-gi brightnessctl
```

From a checked-out repository or unpacked release directory, this block preserves
an existing installation as `zen-brightness.backup` (older backups are numbered)
and stages an atomic replacement:

```sh
(
  set -eu
  install -d "$HOME/.local/bin"
  zen_brightness_target="$HOME/.local/bin/zen-brightness"
  if [ -e "$zen_brightness_target" ]; then
    cp -p --backup=numbered "$zen_brightness_target" "$zen_brightness_target.backup"
  fi
  zen_brightness_stage=$(mktemp "$HOME/.local/bin/.zen-brightness.XXXXXX")
  install -m 0755 ./zen-brightness "$zen_brightness_stage"
  mv -f -- "$zen_brightness_stage" "$zen_brightness_target"
  sha256sum ./zen-brightness "$zen_brightness_target"
  cmp --silent ./zen-brightness "$zen_brightness_target"
)
```

The two hashes must match; `cmp` also returns failure on a byte mismatch. Stop on
any failed installation command. Restore the backup if one was made. On a first
installation without a backup, preserve source and installed copies and do not
bind or run the installed copy until the mismatch/error is resolved.

In GNOME Settings → Keyboard → Custom Shortcuts, bind the brightness keys to
these commands, replacing the example with the actual absolute home path:

```text
/absolute/path/to/.local/bin/zen-brightness up
/absolute/path/to/.local/bin/zen-brightness down
```

Keep existing shortcut/settings values for recovery. Installation does not
change keybindings. Check the installed version with
`~/.local/bin/zen-brightness --version`.

## Tests

Install test dependencies on Ubuntu:

```sh
sudo apt install python3-gi python3-dbus dbus-daemon
```

From the repository root:

```sh
dbus-run-session -- /usr/bin/python3 test_brightness.py
/usr/bin/python3 test_gnome.py
```

The first command runs 22 tests against fake display/notification services on a
private bus, covering concurrent presses, mode changes, stale state, restarts
and failed writes. GitHub Actions runs this suite on Ubuntu 24.04 and 26.04;
that does not qualify those runners' physical displays.

The second command additionally needs installed GNOME Shell, GJS and a working
software-rendering stack. It starts a separate headless compositor and notification
server, with temporary home/config/runtime directories and disabled general D-Bus
autostart. Its brightness method is fake. This optional smoke test was exercised
against GNOME 50.1; it is not part of hosted CI.

Neither test writes to the real panel or restarts the logged-in desktop. A physical
acceptance test needs a separate bounded plan recording/restoring the starting
brightness and defining abort/pass conditions. Tests must pass before installing
a modified candidate.

## Recovery

To restore the most recent saved installation, stage the backup and verify both
hashes match:

```sh
(
  set -eu
  zen_brightness_target="$HOME/.local/bin/zen-brightness"
  test -f "$zen_brightness_target.backup"
  zen_brightness_stage=$(mktemp "$HOME/.local/bin/.zen-brightness.XXXXXX")
  install -m 0755 "$zen_brightness_target.backup" "$zen_brightness_stage"
  mv -f -- "$zen_brightness_stage" "$zen_brightness_target"
  sha256sum "$zen_brightness_target.backup" "$zen_brightness_target"
  cmp --silent "$zen_brightness_target.backup" "$zen_brightness_target"
)
```

If the backup is missing or restoration verification fails, stop and preserve all
copies. Do not select an older numbered backup automatically. Restore changed
shortcuts from their saved values. After replacement, stop pressing keys for five
seconds and check `busctl --user status local.zenbook.Brightness`; if the name is
still owned, inspect that process before testing another version. Do not restart
or log out of the desktop as part of these procedures.

## Maintenance

Keep runtime changes in `zen-brightness` and regression cases in
`test_brightness.py`. After source or GNOME/Mutter upgrades, rerun both tests and
inspect the current display API with these read-only commands:

```sh
gdbus introspect --session --dest org.gnome.Mutter.DisplayConfig --object-path /org/gnome/Mutter/DisplayConfig
gdbus call --session --dest org.gnome.Mutter.DisplayConfig --object-path /org/gnome/Mutter/DisplayConfig --method org.gnome.Mutter.DisplayConfig.GetCurrentState
gdbus call --session --dest org.gnome.Mutter.DisplayConfig --object-path /org/gnome/Mutter/DisplayConfig --method org.freedesktop.DBus.Properties.Get org.gnome.Mutter.DisplayConfig Backlight
```

Compare with `change()` and `panel_info()`: the target must be built in and active;
color modes are 0/default, 1/BT.2100 or 2/SDR native; `Backlight` must contain its
serial and `connector`, `active`, `value`, `min`, `max` fields. Stop deployment on
schema differences or failing tests. Rerunning tests does not establish physical
display acceptance.

For a new release, update `VERSION` in the runtime, the workflow's expected version,
`CHANGELOG.md` and the README's prerelease notice together. Run tests before
tagging that commit; build release assets and checksums from the tagged tree.

## License

[MIT](LICENSE).

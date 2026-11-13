#!/usr/bin/python3
"""Test the candidate's popup on real GNOME, on a private bus/headless display.

Run: /usr/bin/python3 test_gnome.py
The brightness method is replaced with a fake; the real panel is never accessed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import dbus

from test_brightness import load_candidate


def worker(directory, action):
    module = load_candidate()
    module.BACKLIGHT = Path(directory) / "no-real-backlight"
    events = Path(directory) / "events.jsonl"

    def record(event, **values):
        with events.open("a") as stream:
            stream.write(json.dumps(dict(event=event, time=time.monotonic(), **values)) + "\n")

    class ObservedBrightness(module.Brightness):
        def do_startup(self):
            super().do_startup()
            self.connection.signal_subscribe(
                module.NOTIFICATIONS, module.NOTIFICATIONS, "NotificationClosed", module.NOTIFICATION_PATH,
                None, module.Gio.DBusSignalFlags.NONE,
                lambda connection, sender, path, interface, signal, params:
                    record("closed", id=params.unpack()[0], reason=params.unpack()[1]))

        def show(self, title, percent):
            super().show(title, percent)
            record("notify", id=self.notification_id, sender=self.connection.get_unique_name())

        def change(self, direction):
            return "Brightness", 60 if direction == "up" else 50

    module.Brightness = ObservedBrightness
    return module.main(["zen-brightness", action])


def test(directory):
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address or address.startswith("unix:path=/run/user/"):
        raise RuntimeError("Use dbus-run-session for this test")
    root = Path(directory)
    environment = os.environ.copy()
    shell_log = (root / "shell.log").open("w+")
    shell = subprocess.Popen(
        ["gnome-shell", "--headless", "--wayland", "--no-x11",
         "--virtual-monitor", "1280x800", "--wayland-display", "brightness-test"],
        env=environment, stdout=shell_log, stderr=subprocess.STDOUT)
    clients = []
    notifier = None
    try:
        bus = dbus.SessionBus()
        deadline = time.monotonic() + 20
        while True:
            if shell.poll() is not None or time.monotonic() >= deadline:
                shell_log.seek(0)
                raise RuntimeError("Isolated GNOME failed to start:\n" + shell_log.read()[-5000:])
            try:
                if bus.name_has_owner("org.gnome.Shell") and notifier is None:
                    notifier = subprocess.Popen(
                        ["/usr/bin/gjs", "-m", "/usr/share/gnome-shell/org.gnome.Shell.Notifications"],
                        env=environment, stdout=shell_log, stderr=subprocess.STDOUT)
                if bus.name_has_owner("org.freedesktop.Notifications"):
                    obj = bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
                    information = dbus.Interface(obj, "org.freedesktop.Notifications").GetServerInformation(timeout=1)
                    break
            except dbus.DBusException:
                pass
            time.sleep(0.05)
        events_file = root / "events.jsonl"

        def events():
            return [json.loads(line) for line in events_file.read_text().splitlines()] if events_file.exists() else []

        for count, action in enumerate(("down", "up", "down", "up"), start=1):
            clients.append(subprocess.Popen([sys.executable, __file__, "worker", directory, action],
                                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 4
            while len([event for event in events() if event["event"] == "notify"]) < count:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Candidate popup failed")
                time.sleep(0.01)
            time.sleep(0.08)
        for client in clients:
            stdout, stderr = client.communicate(timeout=6)
            assert client.returncode == 0, stderr
        captured = events()
        notifications = [event for event in captured if event["event"] == "notify"]
        closed = [event for event in captured if event["event"] == "closed"]
        assert len(notifications) == 4, captured
        assert len({event["sender"] for event in notifications}) == 1, captured
        assert len({event["id"] for event in notifications}) == 1, captured
        assert len(closed) == 1 and closed[0]["reason"] == 3, captured
        delay = closed[0]["time"] - notifications[-1]["time"]
        assert 0.49 <= delay < 0.8, captured
        result = dict(server=[str(value) for value in information],
                      notifications=len(notifications), owners=1, ids=1,
                      close_reason=3, delay_after_last_press_seconds=round(delay, 3),
                      real_panel_accessed=False, events=captured)
        print(json.dumps(result, indent=2))
    finally:
        for client in clients:
            if client.poll() is None:
                client.terminate()
            client.communicate(timeout=5)
        shell.terminate()
        try:
            shell.wait(timeout=5)
        except subprocess.TimeoutExpired:
            shell.kill()
            shell.wait(timeout=5)
        shell_log.close()
        if notifier is not None:
            notifier.terminate()
            notifier.wait(timeout=5)


def isolated_test():
    # Isolate the bus daemon's environment too, and disable D-Bus autostart.
    # Only the explicitly started compositor, notifier and test clients can run.
    with tempfile.TemporaryDirectory(prefix="zen-brightness-gnome-") as directory:
        root = Path(directory)
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}
        for key, subdir in (("HOME", "home"), ("XDG_RUNTIME_DIR", "runtime"),
                            ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                            ("XDG_CACHE_HOME", "cache")):
            path = root / subdir
            path.mkdir(mode=0o700)
            environment[key] = str(path)
        environment.update(GSETTINGS_BACKEND="memory", LIBGL_ALWAYS_SOFTWARE="1",
                           GIO_USE_VFS="local", GIO_USE_VOLUME_MONITOR="unix",
                           NO_AT_BRIDGE="1", GTK_A11Y="none")
        config = root / "bus.conf"
        config.write_text('''<busconfig>
<type>session</type><listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth>
<policy context="default"><allow own="*"/><allow send_destination="*"/>
<allow receive_sender="*"/></policy>
</busconfig>''')
        result = subprocess.run(["dbus-run-session", "--config-file=" + str(config), "--",
                                 sys.executable, __file__, "inner", directory], env=environment)
        return result.returncode


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        sys.exit(worker(sys.argv[2], sys.argv[3]))
    if len(sys.argv) > 1 and sys.argv[1] == "inner":
        test(sys.argv[2])
    else:
        sys.exit(isolated_test())

#!/usr/bin/python3
"""Run with dbus-run-session -- /usr/bin/python3 test_brightness.py.

The private bus serves fake monitors/notifications and fake sysfs. Every PWM
subprocess is intercepted before it can reach real hardware.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

ROOT = Path(__file__).resolve().parent
CANDIDATE = ROOT / "zen-brightness"
DISPLAY_IFACE = "org.gnome.Mutter.DisplayConfig"
NOTIFY_IFACE = "org.freedesktop.Notifications"
TEST_IFACE = "local.zenbook.TestDesktop"


def load_candidate():
    loader = importlib.machinery.SourceFileLoader("candidate", str(CANDIDATE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeDesktop(dbus.service.Object):
    SUPPORTS_MULTIPLE_OBJECT_PATHS = True
    SUPPORTS_MULTIPLE_CONNECTIONS = True

    def __init__(self, directory):
        self.directory = Path(directory)
        self.bus = dbus.SessionBus()
        self.names = [dbus.service.BusName(name, self.bus) for name in (DISPLAY_IFACE, NOTIFY_IFACE, TEST_IFACE)]
        super().__init__(self.bus, "/org/gnome/Mutter/DisplayConfig")
        self.add_to_connection(self.bus, "/org/freedesktop/Notifications")
        self.events = []
        self.active = {}
        self.next_id = 0
        self.replacement_bus = None
        self.Reset(0, 395, 355)

    def record(self, event, **values):
        self.events.append(dict(event=event, time=time.monotonic(), **values))

    def write_hw(self, value):
        # Model a sysfs attribute read atomically. Truncating a regular file
        # exposes an empty value to concurrent readers, unlike the real device.
        staged = self.directory / ".brightness-next"
        staged.write_text(str(value))
        staged.replace(self.directory / "brightness")
        return GLib.SOURCE_REMOVE

    @dbus.service.method(TEST_IFACE, in_signature="iii", out_signature="")
    def Reset(self, mode, hw, sw):
        if self.replacement_bus is not None:
            self.remove_from_connection(self.replacement_bus, "/org/freedesktop/Notifications")
            self.replacement_bus.close()
            self.replacement_bus = None
        self.bus.request_name(NOTIFY_IFACE)
        self.bus.request_name(DISPLAY_IFACE)
        self.mode, self.sw, self.fault = int(mode), int(sw), ""
        self.events.clear()
        self.active.clear()
        self.write_hw(hw)
        (self.directory / "max_brightness").write_text("400")

    @dbus.service.method(TEST_IFACE, in_signature="s", out_signature="")
    def Fault(self, name):
        self.fault = str(name)
        if name == "missing-mutter":
            self.bus.release_name(DISPLAY_IFACE)
        if name == "missing-notifier":
            self.bus.release_name(NOTIFY_IFACE)

    @dbus.service.method(TEST_IFACE, in_signature="", out_signature="")
    def RestartNotifier(self):
        collision_id = max(self.active)
        self.bus.release_name(NOTIFY_IFACE)
        self.replacement_bus = dbus.bus.BusConnection(os.environ["DBUS_SESSION_BUS_ADDRESS"])
        self.add_to_connection(self.replacement_bus, "/org/freedesktop/Notifications")
        self.replacement_bus.request_name(NOTIFY_IFACE)
        # New server reuses the old number for an unrelated application.
        self.active = {collision_id: ":1.unrelated"}
        self.next_id = collision_id
        self.record("restart", collision_id=collision_id)

    @dbus.service.method(TEST_IFACE, in_signature="", out_signature="")
    def Dismiss(self):
        notification_id = max(self.active)
        self.active.pop(notification_id)
        message = dbus.lowlevel.SignalMessage("/org/freedesktop/Notifications", NOTIFY_IFACE, "NotificationClosed")
        message.append(dbus.UInt32(notification_id), dbus.UInt32(2))
        (self.replacement_bus or self.bus).send_message(message)

    @dbus.service.method(TEST_IFACE, in_signature="", out_signature="s")
    def Events(self):
        return json.dumps(self.events)

    @dbus.service.method(TEST_IFACE, in_signature="i", out_signature="")
    def PWM(self, target):
        if self.fault == "pwm":
            raise dbus.DBusException("PWM write refused", name="local.zenbook.TestError")
        self.write_hw(target)
        self.record("pwm", target=int(target))

    @dbus.service.method(DISPLAY_IFACE, in_signature="", out_signature="ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv}")
    def GetCurrentState(self):
        if self.fault == "timeout":
            time.sleep(2.5)
        monitors = [(("eDP-1", "SDC", "panel", "test"), [],
                     {"is-builtin": dbus.Boolean(True), "color-mode": dbus.UInt32(self.mode)})]
        if self.fault == "no-panel":
            monitors = []
        return 1, monitors, [], {}

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        assert interface == DISPLAY_IFACE and name == "Backlight"
        low, high = (10, 210) if self.mode == 1 else (4, 400)
        if self.fault == "range":
            high = low
        panel = {"connector": "eDP-1", "active": dbus.Boolean(self.fault != "inactive"),
                 "value": dbus.Int32(self.sw), "min": dbus.Int32(low), "max": dbus.Int32(high)}
        return dbus.Struct((dbus.UInt32(2), dbus.Array([panel], signature="a{sv}")),
                           signature="uaa{sv}", variant_level=1)

    @dbus.service.method(DISPLAY_IFACE, in_signature="usi", out_signature="")
    def SetBacklight(self, serial, connector, target):
        assert serial == 2 and connector == "eDP-1"
        if self.fault == "mutter":
            raise dbus.DBusException("Display write refused", name="local.zenbook.TestError")
        # Match Mutter's no-op when it thinks the target is already applied.
        changed = self.sw != target
        if self.fault != "software-stalled":
            self.sw = int(target)
        self.record("mutter", target=int(target), changed=changed)
        if self.mode != 1 and changed and self.fault != "stalled":
            GLib.timeout_add(20, self.write_hw, int(target))

    @dbus.service.method(NOTIFY_IFACE, in_signature="susssasa{sv}i", out_signature="u", sender_keyword="sender")
    def Notify(self, app, replace, icon, title, body, actions, hints, expiry, sender=None):
        if self.fault == "notify":
            raise dbus.DBusException("Notifications unavailable", name="local.zenbook.TestError")
        if replace in self.active and self.active[replace] != sender:
            raise dbus.DBusException("Invalid notification ID", name="org.freedesktop.DBus.Error.InvalidArgs")
        if replace in self.active:
            notification_id = int(replace)
        else:
            self.next_id += 1
            notification_id = self.next_id
        self.active[notification_id] = sender
        self.record("notify", id=notification_id, replaces=int(replace), sender=str(sender),
                    title=str(title), body=str(body), transient=bool(hints["transient"]))
        return notification_id

    @dbus.service.method(NOTIFY_IFACE, in_signature="u", out_signature="", sender_keyword="sender")
    def CloseNotification(self, notification_id, sender=None):
        if notification_id in self.active and self.active[notification_id] != sender:
            raise dbus.DBusException("Invalid notification ID", name="org.freedesktop.DBus.Error.InvalidArgs")
        self.active.pop(notification_id, None)
        self.record("close", id=int(notification_id), sender=str(sender))


def worker(directory, action, gate=None):
    module = load_candidate()
    module.BACKLIGHT = Path(directory)
    DBusGMainLoop(set_as_default=True)

    def fake_pwm(command, **kwargs):
        expected = ["brightnessctl", "--device", "intel_backlight", "--quiet", "set"]
        assert command[:-1] == expected, command
        bus = dbus.SessionBus()
        proxy = dbus.Interface(bus.get_object(TEST_IFACE, "/org/gnome/Mutter/DisplayConfig"), TEST_IFACE)
        try:
            proxy.PWM(int(command[-1]))
        except dbus.DBusException as error:
            raise subprocess.CalledProcessError(1, command, stderr=str(error)) from error
        return subprocess.CompletedProcess(command, 0)

    module.subprocess.run = fake_pwm
    if gate is not None:
        gate = Path(gate)
        gate.with_name(str(os.getpid()) + ".ready").touch()
        deadline = time.monotonic() + 10
        while not gate.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Concurrent test start gate timed out")
            time.sleep(0.01)
    return module.main([str(CANDIDATE), action])


class BrightnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Refuse the real desktop bus, even when someone runs the test incorrectly.
        address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        if not address or address.startswith("unix:path=/run/user/"):
            raise RuntimeError("Use dbus-run-session for these tests")
        cls.directory = tempfile.TemporaryDirectory(prefix="zen-brightness-test-")
        cls.backend = subprocess.Popen([sys.executable, __file__, "desktop", cls.directory.name])
        cls.bus = dbus.SessionBus()
        deadline = time.monotonic() + 5
        while not cls.bus.name_has_owner(TEST_IFACE):
            if time.monotonic() > deadline:
                raise RuntimeError("Fake desktop failed to start")
            time.sleep(0.02)
        cls.desktop = dbus.Interface(cls.bus.get_object(TEST_IFACE, "/org/gnome/Mutter/DisplayConfig"), TEST_IFACE)

    @classmethod
    def tearDownClass(cls):
        cls.backend.terminate()
        cls.backend.wait(timeout=5)
        cls.directory.cleanup()

    def setUp(self):
        self.clients = []
        self.desktop.Reset(0, 395, 355)

    def tearDown(self):
        for client in self.clients:
            client.communicate(timeout=6)

    def launch(self, action):
        client = subprocess.Popen([sys.executable, __file__, "worker", self.directory.name, action],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.clients.append(client)
        return client

    def launch_concurrent(self, count, action):
        # Warm imports first: runner CPU speed must not turn a concurrent burst
        # into several requests separated by more than the popup's lifetime.
        directory = Path(tempfile.mkdtemp(prefix="batch-", dir=self.directory.name))
        gate = directory / "go"
        try:
            for _ in range(count):
                self.clients.append(subprocess.Popen(
                    [sys.executable, __file__, "worker", self.directory.name, action, str(gate)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 8
            while len(list(directory.glob("*.ready"))) != count:
                if time.monotonic() >= deadline:
                    self.fail("Concurrent clients failed to reach the start gate")
                time.sleep(0.01)
        finally:
            gate.touch()

    def events(self, kind=None):
        events = json.loads(self.desktop.Events())
        return [event for event in events if kind is None or event["event"] == kind]

    def wait_events(self, kind, count, timeout=4):
        deadline = time.monotonic() + timeout
        while len(self.events(kind)) < count:
            if time.monotonic() >= deadline:
                self.fail(f"Wanted {count} {kind} events; got {self.events()}")
            time.sleep(0.01)
        return self.events(kind)

    def finish(self):
        results = []
        for client in self.clients:
            stdout, stderr = client.communicate(timeout=6)
            results.append((client.returncode, stderr))
        self.clients.clear()
        self.assertTrue(all(code == 0 for code, _ in results),
                        f"Client results: {results}; events: {self.events()}")

    def test_sdr_up_stays_at_max_instead_of_reversing(self):
        self.launch("up")
        self.wait_events("notify", 1)
        for _ in range(5):
            self.launch("up")
        self.finish()
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 400)
        self.assertEqual(len(self.events("notify")), 6)
        self.assertTrue(all(e["title"] == "Brightness" and e["body"] == "100%" for e in self.events("notify")))

    def test_sdr_stale_cache_equal_to_target_still_changes_hardware(self):
        self.launch("down")  # HW 395 -> 355, but Mutter already caches 355.
        self.finish()
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 355)
        self.assertEqual(self.events("pwm")[0]["target"], 355)

    def test_24_concurrent_keys_have_one_owner_and_no_lost_steps(self):
        self.launch_concurrent(24, "down")
        self.finish()
        self.assertEqual(len(self.events("notify")), 24)
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 4)
        targets = [e["target"] for e in self.events() if e["event"] in ("mutter", "pwm")]
        self.assertTrue(all(a >= b for a, b in zip(targets, targets[1:])), targets)
        senders = {e["sender"] for e in self.events("notify")}
        self.assertEqual(len(senders), 1)
        ids = {e["id"] for e in self.events("notify")}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(self.events("close")), 1)
        self.assertIn(self.events("close")[0]["sender"], senders)

    def test_seven_concurrent_keys_preserve_every_step_before_floor(self):
        self.desktop.Reset(0, 400, 400)
        self.launch_concurrent(7, "down")
        self.finish()
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 120)
        self.assertEqual([e["target"] for e in self.events("mutter")],
                         [360, 320, 280, 240, 200, 160, 120])

    def test_new_burst_after_exit_uses_a_fresh_notification(self):
        self.launch("down")
        self.finish()
        self.assertFalse(self.bus.name_has_owner("local.zenbook.Brightness"))
        self.launch("up")
        self.finish()
        notifications = self.events("notify")
        self.assertEqual(len(notifications), 2)
        self.assertTrue(all(event["replaces"] == 0 for event in notifications))
        self.assertNotEqual(notifications[0]["sender"], notifications[1]["sender"])
        self.assertEqual(len(self.events("close")), 2)

    def test_popup_closes_half_second_after_last_key_and_restarts_cleanly(self):
        self.launch("down")
        self.wait_events("notify", 1)
        time.sleep(0.3)
        self.launch("down")
        self.wait_events("notify", 2)
        time.sleep(0.3)
        self.assertEqual(self.events("close"), [])
        close = self.wait_events("close", 1)[0]
        delay = close["time"] - self.events("notify")[-1]["time"]
        self.assertGreaterEqual(delay, 0.49)
        self.assertLess(delay, 0.8)
        self.launch("up")  # Same application, after the preceding popup closed.
        notifications = self.wait_events("notify", 3)
        self.assertEqual(notifications[-1]["replaces"], 0)
        self.assertNotEqual(notifications[-1]["id"], notifications[0]["id"])
        self.finish()
        self.assertEqual(len(self.events("close")), 2)
        self.assertFalse(self.bus.name_has_owner("local.zenbook.Brightness"))

    def test_hdr_software_floor_then_pwm_and_reverse(self):
        self.desktop.Reset(1, 400, 30)
        for action in ("down", "down", "up", "up"):
            self.launch(action)
            self.wait_events("notify", len(self.clients))
        self.finish()
        writes = [(e["event"], e["target"]) for e in self.events() if e["event"] in ("mutter", "pwm")]
        self.assertEqual(writes, [("mutter", 10), ("pwm", 360), ("pwm", 400), ("mutter", 30)])
        self.assertEqual([e["title"] for e in self.events("notify")],
                         ["SDR intensity", "Extra dark (PWM)", "Extra dark (PWM)", "SDR intensity"])

    def test_mode_switch_during_burst_uses_fresh_mode(self):
        self.desktop.Reset(1, 400, 30)
        self.launch("down")
        self.wait_events("notify", 1)
        self.desktop.Reset(0, 400, 400)
        self.launch("down")
        self.finish()
        self.assertEqual(self.events("notify")[-1]["title"], "Brightness")
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 360)

    def test_notification_failure_does_not_fail_brightness(self):
        self.desktop.Fault("notify")
        client = self.launch("up")
        stdout, stderr = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 0)
        self.assertIn("popup failed", stderr)
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 400)

    def test_notification_restart_does_not_replace_another_apps_id(self):
        self.launch("down")
        self.wait_events("notify", 1)
        self.desktop.RestartNotifier()
        self.launch("up")
        self.finish()
        notifications = self.events("notify")
        self.assertEqual(len(notifications), 2)
        self.assertEqual(notifications[1]["replaces"], 0)
        self.assertNotEqual(notifications[1]["id"], notifications[0]["id"])

    def test_manually_dismissed_notification_is_not_reused(self):
        self.launch("down")
        self.wait_events("notify", 1)
        self.desktop.Dismiss()
        time.sleep(0.05)
        self.launch("up")
        self.finish()
        self.assertEqual(self.events("notify")[-1]["replaces"], 0)

    def test_unavailable_mutter_fails_with_nonzero_status(self):
        self.desktop.Fault("missing-mutter")
        client = self.launch("up")
        stdout, stderr = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 1, stderr)
        self.assertEqual(self.events(), [])

    def test_missing_notification_service_does_not_stop_brightness(self):
        self.desktop.Fault("missing-notifier")
        client = self.launch("up")
        stdout, stderr = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 0, stderr)
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 400)
        self.assertEqual(self.events("notify"), [])

    def test_sdr_native_mode_uses_the_hardware_path(self):
        self.desktop.Reset(2, 400, 400)
        self.launch("down")
        self.finish()
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 360)
        self.assertEqual(self.events("notify")[0]["title"], "Brightness")

    def test_slow_dbus_call_has_a_bounded_timeout(self):
        self.desktop.Fault("timeout")
        start = time.monotonic()
        client = self.launch("up")
        stdout, stderr = client.communicate(timeout=6)
        duration = time.monotonic() - start
        self.assertEqual(client.returncode, 1, stderr)
        self.assertLess(duration, 4.5)
        self.assertEqual(self.events(), [])

    def test_unknown_display_mode_fails_before_writing(self):
        self.desktop.Reset(99, 400, 400)
        client = self.launch("down")
        stdout, stderr = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 1, stderr)
        self.assertEqual(self.events(), [])

    def test_unaccepted_hdr_software_write_does_not_claim_success(self):
        self.desktop.Reset(1, 400, 30)
        self.desktop.Fault("software-stalled")
        client = self.launch("down")
        stdout, stderr = client.communicate(timeout=6)
        self.assertEqual(client.returncode, 1, stderr)
        self.assertEqual(self.events("notify"), [])

    def test_invalid_action_does_not_dim(self):
        client = self.launch("typo")
        client.communicate(timeout=6)
        self.assertEqual(client.returncode, 2)
        self.assertEqual(self.events(), [])

    def test_unavailable_panel_and_writes_fail_without_popup(self):
        for fault in ("no-panel", "inactive", "range", "mutter", "stalled"):
            with self.subTest(fault=fault):
                self.desktop.Reset(0, 395, 355)
                self.desktop.Fault(fault)
                client = self.launch("up")
                stdout, stderr = client.communicate(timeout=6)
                self.assertEqual(client.returncode, 1, stderr)
                self.assertEqual(self.events("notify"), [])

    def test_hdr_pwm_failure_does_not_claim_success(self):
        self.desktop.Reset(1, 200, 10)
        self.desktop.Fault("pwm")
        client = self.launch("down")
        client.communicate(timeout=6)
        self.assertEqual(client.returncode, 1)
        self.assertEqual(self.events("notify"), [])
        self.assertEqual(int(Path(self.directory.name, "brightness").read_text()), 200)

    def test_monotonic_steps_and_bounds_across_ranges(self):
        step = load_candidate().next_step
        for hdr in (False, True):
            for hw in range(4, 401):
                for sw in (10, 30, 110, 210):
                    for direction in ("up", "down"):
                        control, target, title, percent = step(direction, hw, 400, hdr, sw, 10, 210)
                        current = sw if hdr and control == "mutter" else hw
                        self.assertTrue(target >= current if direction == "up" else target <= current)
                        self.assertGreaterEqual(target, 10 if hdr and control == "mutter" else 4)
                        self.assertLessEqual(target, 210 if hdr and control == "mutter" else 400)
                        self.assertTrue(0 <= percent <= 100)

    def test_dim_never_brightens_an_externally_zeroed_backlight(self):
        step = load_candidate().next_step
        for hw in range(4):
            with self.subTest(hw=hw):
                with self.assertRaises(ValueError):
                    step("down", hw, 400, False, hw, 4, 400)
                self.assertGreater(step("up", hw, 400, False, hw, 4, 400)[1], hw)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "desktop":
        DBusGMainLoop(set_as_default=True)
        desktop = FakeDesktop(sys.argv[2])
        GLib.MainLoop().run()
    elif len(sys.argv) > 1 and sys.argv[1] == "worker":
        sys.exit(worker(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None))
    else:
        unittest.main(verbosity=2)

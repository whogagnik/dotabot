import ctypes
import importlib
import logging
import sys
import time
import types
import unittest


if not hasattr(ctypes, "windll"):
    ctypes.windll = types.SimpleNamespace(user32=object())
elif not hasattr(ctypes.windll, "user32"):
    ctypes.windll.user32 = object()


class DummyAccount:
    def __init__(
        self,
        username,
        password,
        logger=None,
        placer=None,
        status_cb=None,
        thread_registry=None,
    ):
        self.username = username
        self.password = password
        self.logger = logger
        self.placer = placer
        self.status_cb = status_cb
        self.thread_registry = thread_registry
        self.mafile_path = None
        self.mafile_data = None
        self.status = "idle"

    def attach_mafile(self, path, data):
        self.mafile_path = path
        self.mafile_data = data

    def to_state_dict(self):
        return {
            "username": self.username,
            "password": self.password,
            "mafile_path": self.mafile_path,
            "mafile_data": self.mafile_data,
        }

    @classmethod
    def from_state_dict(cls, data, logger=None, status_cb=None):
        acc = cls(data["username"], data["password"], logger, None, status_cb, None)
        acc.mafile_path = data.get("mafile_path")
        acc.mafile_data = data.get("mafile_data")
        return acc

    def get_steamid3(self):
        return 123


class DummyPlannerRuntime:
    def __init__(self):
        self.registered = []
        self.ticks = 0
        self.attach_calls = []

    def register_vm(self, vm_id):
        self.registered.append(vm_id)

    def tick_all(self):
        self.ticks += 1

    def attach_hwnds(self, **kwargs):
        self.attach_calls.append(kwargs)
        return None


def install_import_stubs():
    account_mod = types.ModuleType("scripts.host.core.account")
    account_mod.Account = DummyAccount
    sys.modules["scripts.host.core.account"] = account_mod

    planner_mod = types.ModuleType("scripts.host.game.planner_runtime")
    planner_mod.planner_runtime = DummyPlannerRuntime()
    sys.modules["scripts.host.game.planner_runtime"] = planner_mod

    start_mm_mod = types.ModuleType("scripts.host.game.start_mm_dota2")
    start_mm_mod.StartMmDota2 = None
    sys.modules["scripts.host.game.start_mm_dota2"] = start_mm_mod

    cv2_mod = types.ModuleType("cv2")
    cv2_mod.IMREAD_GRAYSCALE = 0
    cv2_mod.COLOR_RGB2GRAY = 0
    cv2_mod.TM_CCOEFF_NORMED = 0
    cv2_mod.imread = lambda *args, **kwargs: None
    cv2_mod.cvtColor = lambda image, code: image
    cv2_mod.matchTemplate = lambda *args, **kwargs: []
    cv2_mod.minMaxLoc = lambda *args, **kwargs: (0, 0, (0, 0), (0, 0))
    sys.modules["cv2"] = cv2_mod

    np_mod = types.ModuleType("numpy")
    np_mod.ndarray = object
    np_mod.uint8 = object
    np_mod.array = lambda image, dtype=None: image
    sys.modules["numpy"] = np_mod

    pil_mod = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")
    image_mod.open = lambda *args, **kwargs: None
    pil_mod.Image = image_mod
    sys.modules["PIL"] = pil_mod
    sys.modules["PIL.Image"] = image_mod


install_import_stubs()
controller_mod = importlib.import_module("scripts.host.app.controller")


class ControllerStabilityTests(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger(f"controller-test-{id(self)}")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        self.controller = controller_mod.Controller(logger, lambda *_: None)
        self.controller.mm_starter = None
        self.controller.accounts.clear()
        if hasattr(controller_mod.planner_runtime, "attach_calls"):
            controller_mod.planner_runtime.attach_calls.clear()

    def add_vm_account(self, has_mafile=False):
        account = DummyAccount("alice", "secret")
        if has_mafile:
            account.attach_mafile("alice.mafile", {"Session": {"AccessToken": "token"}})
        self.controller.accounts.append(account)

        vm = self.controller.register_vm()
        acc = controller_mod.VmAccountState(
            username="alice",
            password="secret",
            mafile_path=account.mafile_path,
            has_mafile=has_mafile,
        )
        vm.assigned_accounts = [acc]
        return vm, acc

    def test_sent_command_is_not_redelivered_before_timeout(self):
        vm = self.controller.register_vm()
        cmd = self.controller._push_command(
            vm,
            controller_mod.HostCommandType.LAUNCH_PROCESS,
            {"account_login": "alice"},
        )

        first = self.controller.get_next_command(vm.vm_id)

        self.assertEqual(first["id"], cmd.id)
        self.assertIsNone(self.controller.get_next_command(vm.vm_id))
        self.assertEqual(vm.current_command_id, cmd.id)

    def test_expire_stale_command_clears_current_command(self):
        vm = self.controller.register_vm()
        cmd = self.controller._push_command(
            vm,
            controller_mod.HostCommandType.SLEEP,
            {"duration_ms": 1},
        )
        self.controller.get_next_command(vm.vm_id)
        cmd.sent_ts = time.time() - 60

        self.controller._expire_stale_commands()

        self.assertIsNone(vm.current_command_id)
        self.assertEqual(vm.command_queue, [])

    def test_find_dota_expiration_respects_payload_timeout(self):
        vm = self.controller.register_vm()
        cmd = self.controller._push_command(
            vm,
            controller_mod.HostCommandType.FIND_DOTA_WINDOW,
            {"timeout_ms": 60000},
        )
        self.controller.get_next_command(vm.vm_id)
        cmd.sent_ts = time.time() - 10

        self.controller._expire_stale_commands()

        self.assertEqual(vm.current_command_id, cmd.id)
        self.assertEqual(vm.command_queue, [cmd])

    def test_expired_login_window_search_is_retried_not_vm_error(self):
        vm, acc = self.add_vm_account(has_mafile=False)
        acc.launched = True
        acc.login_find_in_progress = True
        cmd = self.controller._push_command(
            vm,
            controller_mod.HostCommandType.FIND_LOGIN_WINDOW,
            {"account_login": "alice", "timeout_ms": 1},
        )
        self.controller.get_next_command(vm.vm_id)
        cmd.sent_ts = time.time() - 40

        self.controller._expire_stale_commands()

        self.assertFalse(acc.login_find_in_progress)
        self.assertNotEqual(vm.status, controller_mod.VmStatus.ERROR)
        self.controller.drive_vm_bootstrap()
        self.assertEqual(
            vm.command_queue[0].type,
            controller_mod.HostCommandType.FIND_LOGIN_WINDOW,
        )

    def test_password_auth_queues_client_sleep_without_blocking_controller(self):
        vm, acc = self.add_vm_account(has_mafile=False)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100

        started = time.monotonic()
        self.controller.drive_vm_bootstrap()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)
        self.assertEqual(
            [cmd.type for cmd in vm.command_queue],
            [
                controller_mod.HostCommandType.FOCUS_WINDOW,
                controller_mod.HostCommandType.SLEEP,
                controller_mod.HostCommandType.CAPTURE_DESKTOP,
            ],
        )
        focus_commands = [
            command for command in vm.command_queue
            if command.type == controller_mod.HostCommandType.FOCUS_WINDOW
        ]
        self.assertEqual([command.payload["settle_ms"] for command in focus_commands], [100])
        self.assertFalse(acc.auth_done)

    def test_failed_auth_command_cancels_queued_tail_and_refinds_login(self):
        vm, acc = self.add_vm_account(has_mafile=False)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100

        self.controller.drive_vm_bootstrap()
        failed = vm.command_queue[0]
        failed.status = "failed"
        failed.result = {"error": "focus failed"}

        self.controller._handle_command_result(vm, failed)

        self.assertFalse(acc.login_window_found)
        self.assertIsNone(acc.login_hwnd)
        self.assertEqual(vm.command_queue, [])
        self.assertNotEqual(vm.status, controller_mod.VmStatus.ERROR)

    def test_recovered_focus_updates_queued_login_capture_hwnd(self):
        vm, acc = self.add_vm_account(has_mafile=True)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100
        vm.login_hwnds = [100]
        self.controller.drive_vm_bootstrap()
        focus, capture = vm.command_queue[-2:]
        self.assertEqual(capture.payload["hwnd"], 100)

        focus.status = "done"
        focus.result = {"hwnd": 200, "replaced_hwnd": 100}
        self.controller._handle_command_result(vm, focus)

        self.assertEqual(acc.login_hwnd, 200)
        self.assertEqual(vm.login_hwnds, [200])
        self.assertEqual(capture.payload["hwnd"], 200)

    def test_password_auth_clicks_login_or_password_before_typing(self):
        vm, acc = self.add_vm_account(has_mafile=False)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100
        self.controller.drive_vm_bootstrap()
        capture = vm.command_queue[-1]
        capture.status = "done"
        capture.result = {"image_b64": "not-needed-for-mocked-match"}
        self.controller._store_desktop_frame_from_result = lambda *_: None
        self.controller._find_desktop_steam_popup_match = lambda *args, **kwargs: {
            "x": 500, "y": 300, "score": 0.95,
        }
        self.controller._handle_command_result(vm, capture)
        click = vm.command_queue[-1]
        self.assertEqual(click.type, controller_mod.HostCommandType.MOUSE_CLICK)
        self.assertEqual(click.payload["purpose"], "auth_login_field")
        self.assertEqual((click.payload["x"], click.payload["y"]), (500, 300))
        self.assertFalse(click.payload["force_fg"])
        self.assertTrue(click.payload["foreground_only"])
        self.assertNotIn("hwnd", click.payload)
        click.status = "done"
        click.result = {}
        self.controller._handle_command_result(vm, click)
        self.assertTrue(acc.auth_login_field_clicked)
        self.assertTrue(acc.login_window_found)
        self.assertEqual(acc.login_hwnd, 100)
        settle = vm.command_queue[-1]
        self.assertEqual(settle.type, controller_mod.HostCommandType.SLEEP)
        self.assertEqual(settle.payload["duration_ms"], 3000)
        self.assertEqual(settle.payload["purpose"], "auth_login_field_settle")

        # Mark the old commands complete, then use the still-focused Steam
        # form for credentials without activating or refinding it.
        for command in vm.command_queue:
            command.status = "done"
        vm.current_command_id = None
        self.controller.drive_vm_bootstrap()
        types = [command.type for command in vm.command_queue if command.status == "queued"]
        self.assertEqual(types, [
            controller_mod.HostCommandType.WRITE_TEXT,
            controller_mod.HostCommandType.KEY_PRESS,
            controller_mod.HostCommandType.WRITE_TEXT,
            controller_mod.HostCommandType.KEY_PRESS,
        ])
        credential_commands = [
            command for command in vm.command_queue
            if command.status == "queued" and command.type in {
                controller_mod.HostCommandType.WRITE_TEXT,
                controller_mod.HostCommandType.KEY_PRESS,
            }
        ]
        self.assertTrue(all(command.payload["force_fg"] is False for command in credential_commands))
        self.assertTrue(all(command.payload["foreground_only"] for command in credential_commands))

    def test_mafile_auth_in_progress_prevents_capture_loop(self):
        vm, acc = self.add_vm_account(has_mafile=True)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100
        acc.auth_flow_in_progress = True
        acc.auth_flow_started_ts = time.time()

        self.controller.drive_vm_bootstrap()

        self.assertEqual(vm.command_queue, [])

    def test_mafile_capture_starts_single_auth_job(self):
        vm, acc = self.add_vm_account(has_mafile=True)
        acc.auth_branch = "mafile"
        acc.auth_capture_requested = True
        calls = []

        def fake_start(vm_arg, acc_arg, image_b64):
            calls.append((vm_arg.vm_id, acc_arg.username, image_b64))
            acc_arg.auth_flow_in_progress = True

        self.controller._start_mafile_auth_job = fake_start
        cmd = controller_mod.VmCommand(
            id=1,
            type=controller_mod.HostCommandType.CAPTURE_FRAME,
            payload={"account_login": "alice", "purpose": "auth_qr"},
            created_ts=time.time(),
            status="done",
            result={"image_b64": "abc"},
        )

        self.controller._handle_command_result(vm, cmd)

        self.assertEqual(calls, [(vm.vm_id, "alice", "abc")])
        self.assertFalse(acc.auth_capture_requested)
        self.assertTrue(acc.auth_flow_in_progress)

    def test_wait_dota_tries_window_find_before_first_desktop_capture(self):
        vm, acc = self.add_vm_account(has_mafile=False)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100
        acc.auth_done = True

        self.controller.drive_vm_bootstrap()

        self.assertEqual(len(vm.command_queue), 1)
        self.assertEqual(
            vm.command_queue[0].type,
            controller_mod.HostCommandType.FIND_DOTA_WINDOW,
        )

    def test_wait_dota_does_not_repeat_desktop_capture_before_find(self):
        vm, acc = self.add_vm_account(has_mafile=False)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100
        acc.auth_done = True
        acc.last_popup_scan_ts = time.time()
        self.controller._desktop_frames[vm.vm_id] = object()
        self.controller._desktop_frame_ts[vm.vm_id] = time.time()

        self.controller.drive_vm_bootstrap()

        self.assertEqual(len(vm.command_queue), 1)
        self.assertEqual(
            vm.command_queue[0].type,
            controller_mod.HostCommandType.FIND_DOTA_WINDOW,
        )

    def test_empty_process_tree_after_auth_resets_for_relaunch(self):
        vm, acc = self.add_vm_account(has_mafile=False)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100
        acc.auth_done = True
        acc.dota_wait_started_ts = time.time() - 30
        acc.last_dota_find_ts = time.time()
        acc.popup_capture_requested = True
        self.controller._desktop_frames[vm.vm_id] = object()
        self.controller._desktop_frame_ts[vm.vm_id] = time.time()

        cmd = controller_mod.VmCommand(
            id=1,
            type=controller_mod.HostCommandType.FIND_DOTA_WINDOW,
            payload={"account_login": "alice"},
            created_ts=time.time(),
            status="done",
            result={
                "found": False,
                "hwnd": None,
                "pid": None,
                "tree_pids": [],
                "process_tree_alive": False,
            },
        )

        self.controller._handle_command_result(vm, cmd)

        self.assertFalse(acc.launched)
        self.assertFalse(acc.auth_done)
        self.assertFalse(acc.popup_capture_requested)
        self.assertEqual(acc.last_dota_find_ts, 0.0)
        self.assertNotIn(vm.vm_id, self.controller._desktop_frames)

        self.controller.drive_vm_bootstrap()

        self.assertEqual(len(vm.command_queue), 1)
        self.assertEqual(
            vm.command_queue[0].type,
            controller_mod.HostCommandType.LAUNCH_PROCESS,
        )

    def test_wait_dota_timeout_restarts_steam_before_relaunch(self):
        vm, acc = self.add_vm_account(has_mafile=False)
        acc.launched = True
        acc.login_window_found = True
        acc.login_hwnd = 100
        acc.auth_done = True
        acc.dota_wait_started_ts = time.time() - (
            self.controller.dota_wait_timeout_sec + 1
        )
        acc.last_dota_find_ts = time.time() - 30
        acc.last_popup_scan_ts = time.time() - 30

        self.controller.drive_vm_bootstrap()

        self.assertTrue(acc.steam_restart_pending)
        self.assertEqual(len(vm.command_queue), 1)
        self.assertEqual(
            vm.command_queue[0].type,
            controller_mod.HostCommandType.KILL_PROCESS_TREE,
        )

        kill = vm.command_queue[0]
        kill.status = "done"
        kill.result = {"account_login": acc.username, "killed": True}
        self.controller._handle_command_result(vm, kill)

        self.assertFalse(acc.steam_restart_pending)
        self.assertFalse(acc.launched)
        self.assertFalse(acc.auth_done)
        self.assertEqual(acc.dota_wait_fail_count, 1)

    def test_window_arrangement_waits_for_all_acks(self):
        vm = self.controller.register_vm()
        vm.dota_hwnds = [10, 20]

        self.controller.arrange_dota_windows(vm)

        self.assertFalse(vm.windows_arranged)
        self.assertTrue(vm.windows_arrange_sent)
        self.assertEqual(len(vm.windows_arrange_pending_ids), 2)
        self.assertEqual(
            [cmd.type for cmd in vm.command_queue],
            [
                controller_mod.HostCommandType.MOVE_WINDOW,
                controller_mod.HostCommandType.FOCUS_WINDOW,
                controller_mod.HostCommandType.MOVE_WINDOW,
                controller_mod.HostCommandType.FOCUS_WINDOW,
            ],
        )

        first = vm.command_queue[0]
        first.status = "done"
        first.result = {}
        self.controller._handle_command_result(vm, first)

        self.assertFalse(vm.windows_arranged)
        self.assertTrue(vm.windows_arrange_sent)

        focus_first = vm.command_queue[0]
        focus_first.status = "done"
        focus_first.result = {}
        self.controller._handle_command_result(vm, focus_first)

        second = vm.command_queue[0]
        second.status = "done"
        second.result = {}
        self.controller._handle_command_result(vm, second)

        self.assertTrue(vm.windows_arranged)
        self.assertFalse(vm.windows_arrange_sent)

        focus_second = vm.command_queue[0]
        focus_second.status = "done"
        focus_second.result = {}
        self.controller._handle_command_result(vm, focus_second)

        self.assertEqual(vm.command_queue[0].type, controller_mod.HostCommandType.SLEEP)
        self.assertEqual(vm.command_queue[0].payload["duration_ms"], 90000)
        self.assertEqual(vm.command_queue[0].payload["purpose"], "arrange_dota_windows_settle")
        settle = vm.command_queue[0]
        settle.status = "done"
        settle.result = {}
        self.controller._handle_command_result(vm, settle)
        self.assertEqual(vm.command_queue, [])

    def test_mm_roles_are_forwarded_to_planner_runtime(self):
        vm = self.controller.register_vm()
        self.controller.accounts.extend(
            [
                DummyAccount("alice", "secret"),
                DummyAccount("bob", "secret"),
            ]
        )

        acc1 = controller_mod.VmAccountState(username="alice", password="secret")
        acc1.dota_window_found = True
        acc1.dota_hwnd = 10
        acc1.dota_pid = 100

        acc2 = controller_mod.VmAccountState(username="bob", password="secret")
        acc2.dota_window_found = True
        acc2.dota_hwnd = 20
        acc2.dota_pid = 200

        vm.assigned_accounts = [acc1, acc2]
        vm.dota_hwnds = [10, 20]
        vm.windows_arranged = True

        class FakeMmStarter:
            def tick_one(self, vm_id, hwnds, *, friend_ids=None):
                return True

            def get_stage(self, vm_id):
                return "done"

            def get_side(self, vm_id):
                return "dire"

            def get_roles(self, vm_id, hwnds=None):
                return ["Carry", "Hard Support"]

        self.controller.mm_starter = FakeMmStarter()

        self.controller.activate_planners_for_ready_vms()

        self.assertTrue(vm.planner_active)
        self.assertEqual(vm.side, "dire")
        self.assertEqual(vm.roles, ["Carry", "Hard Support"])
        self.assertEqual(
            controller_mod.planner_runtime.attach_calls[-1]["roles"],
            ["Carry", "Hard Support"],
        )


if __name__ == "__main__":
    unittest.main()

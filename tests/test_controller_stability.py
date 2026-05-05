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

    def register_vm(self, vm_id):
        self.registered.append(vm_id)

    def tick_all(self):
        self.ticks += 1

    def attach_hwnds(self, **kwargs):
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
                controller_mod.HostCommandType.WRITE_TEXT,
                controller_mod.HostCommandType.KEY_PRESS,
                controller_mod.HostCommandType.WRITE_TEXT,
                controller_mod.HostCommandType.KEY_PRESS,
            ],
        )
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

    def test_window_arrangement_waits_for_all_acks(self):
        vm = self.controller.register_vm()
        vm.dota_hwnds = [10, 20]

        self.controller.arrange_dota_windows(vm)

        self.assertFalse(vm.windows_arranged)
        self.assertTrue(vm.windows_arrange_sent)
        self.assertEqual(len(vm.windows_arrange_pending_ids), 2)

        first = vm.command_queue[0]
        first.status = "done"
        first.result = {}
        self.controller._handle_command_result(vm, first)

        self.assertFalse(vm.windows_arranged)
        self.assertTrue(vm.windows_arrange_sent)

        second = vm.command_queue[0]
        second.status = "done"
        second.result = {}
        self.controller._handle_command_result(vm, second)

        self.assertTrue(vm.windows_arranged)
        self.assertFalse(vm.windows_arrange_sent)
        self.assertEqual(vm.command_queue, [])


if __name__ == "__main__":
    unittest.main()

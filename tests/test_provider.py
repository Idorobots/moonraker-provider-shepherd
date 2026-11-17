# SPDX-License-Identifier: MIT
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from support import Config, ServerError, inventory, load_provider

ShepherdProvider = load_provider()


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config()
        self.provider = ShepherdProvider(self.config)
        self.provider._request = AsyncMock(return_value=inventory())

    async def test_initialization_and_state_notifications(self):
        await self.provider.initialize()
        self.assertEqual(len(self.config.server.callbacks), 1)
        self.assertEqual(self.config.server.events, [])
        self.provider._request.return_value = inventory("inactive")
        await self.provider._update_service_status(2)
        self.assertEqual(self.config.server.events[-1], (
            "machine:service_state_changed", {"klipper": {
                "active_state": "inactive", "sub_state": "dead"}}))
        count = len(self.config.server.events)
        await self.provider._update_service_status(4)
        self.assertEqual(len(self.config.server.events), count)

    async def test_failure_marks_state_unknown_then_recovers(self):
        await self.provider._refresh()
        self.provider._request.side_effect = ServerError("unavailable")
        with self.assertLogs(level="ERROR"):
            await self.provider._update_service_status(2)
        self.assertEqual(self.provider.available_services["klipper"]["sub_state"], "unavailable")
        self.provider._request.side_effect = None
        self.provider._request.return_value = inventory()
        await self.provider._update_service_status(4)
        self.assertEqual(self.provider.available_services["klipper"]["active_state"], "active")

    async def test_initial_failure_still_registers_polling(self):
        self.provider._request.side_effect = ServerError("sudo denied")
        await self.provider.initialize()
        self.assertEqual(len(self.config.server.warnings), 1)
        self.assertEqual(len(self.config.server.callbacks), 1)

    async def test_removed_services_update_system_info(self):
        await self.provider._refresh()
        self.provider._request.return_value = {"services": {}}
        await self.provider._refresh()
        self.assertEqual(self.config.server.components["machine"].system_info["available_services"], [])
        self.assertEqual(self.config.server.events[-1][1]["klipper"]["sub_state"], "not-found")

    async def test_service_identity_uses_pid_and_filters_properties(self):
        result = await self.provider.extract_service_info("other", 42, ["Description"])
        self.assertEqual(result, {"manager": "shepherd", "unit_name": "klipper",
                                 "is_default": False, "properties": {"Description": "Printer"}})
        self.assertEqual(await self.provider.extract_service_info("klipper", 9), {})

    async def test_metadata_refresh_publishes_changes_once(self):
        for result, state in (
                (inventory("inactive", pid=None), {"active_state": "inactive", "sub_state": "dead"}),
                ({"services": {}}, {"active_state": "unknown", "sub_state": "not-found"})):
            with self.subTest(state=state):
                self.provider._request.return_value = inventory()
                await self.provider._refresh(notify=False)
                self.config.server.events.clear()
                self.provider._request.return_value = result
                await self.provider.extract_service_info("klipper", 42)
                await self.provider._update_service_status(2)
                self.assertEqual(self.config.server.events, [(
                    "machine:service_state_changed", {"klipper": state})])

    async def test_action_preflight_does_not_swallow_changes(self):
        await self.provider._refresh(notify=False)
        self.provider._request.side_effect = [inventory("inactive"), {"accepted": False}, inventory("inactive")]
        await self.provider.do_service_action("stop", "klipper")
        self.assertEqual(self.config.server.events, [(
            "machine:service_state_changed", {"klipper": {
                "active_state": "inactive", "sub_state": "dead"}})])

    async def test_actions_require_current_helper_permissions(self):
        await self.provider.do_service_action("stop", "klipper")
        self.provider._request.assert_any_await("stop", "klipper")
        for action, name in (("stop", "klipper; reboot"), ("eval", "klipper"), ("restart", "root")):
            with self.subTest(action=action, name=name), self.assertRaises(ServerError):
                await self.provider.do_service_action(action, name)

    async def test_queued_self_restart_does_not_wait_for_service(self):
        own = inventory()["services"]["klipper"]
        own["actions"] = ["restart"]
        self.provider._request.side_effect = [{"services": {"moonraker": own}}, {"accepted": True}]
        await self.provider.do_service_action("restart", "moonraker")
        self.assertEqual(self.provider._request.await_count, 2)

    async def test_power_and_capability_calls(self):
        await self.provider.shutdown()
        self.provider._request.assert_awaited_with("poweroff")
        await self.provider.reboot()
        self.provider._request.assert_awaited_with("reboot")
        self.assertTrue(await self.provider.check_sudo_access())
        self.provider._request.assert_awaited_with("capabilities")
        self.provider._request.side_effect = ServerError("denied")
        self.assertFalse(await self.provider.check_sudo_access())

    async def test_virtualization_preserves_detected_information(self):
        info = self.config.server.components["machine"].system_info["virtualization"]
        info.update(virt_type="vm", virt_identifier="kvm")
        self.assertEqual(await self.provider.check_virt_status(), info)

    async def test_invalid_inventory_does_not_replace_good_state(self):
        await self.provider._refresh()
        self.provider._request.return_value = {"services": {"klipper": {"active_state": 42}}}
        with self.assertRaises(ServerError):
            await self.provider._refresh()
        self.assertEqual(self.provider.available_services["klipper"]["active_state"], "active")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = ShepherdProvider(Config())
        self.process = AsyncMock()
        self.process.returncode = 0
        self.output = json.dumps({"protocol": 1, "ok": True, "result": {}}).encode()
        self.errors = b""
        self.process.kill = unittest.mock.Mock()
        async def spawn(*args, **kwargs):
            kwargs["stdout"].write(self.output)
            kwargs["stderr"].write(self.errors)
            return self.process
        self.spawn = patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn))
        self.mock_spawn = self.spawn.start()
        self.addCleanup(self.spawn.stop)

    async def test_argument_vector_and_noninteractive_sudo(self):
        await self.provider._request("restart", "klipper")
        args, kwargs = self.mock_spawn.call_args
        self.assertEqual(args, (self.provider.sudo, "-n", "--", self.provider.helper,
                                self.provider.policy, "restart", "klipper"))
        self.assertEqual(kwargs["stdin"], asyncio.subprocess.DEVNULL)
        self.assertTrue(kwargs["stdout"].closed)
        self.assertTrue(kwargs["stderr"].closed)

    async def test_denied_response_and_invalid_protocol(self):
        self.process.returncode = 1
        self.output = json.dumps({
            "protocol": 1, "ok": False,
            "error": {"code": "denied", "message": "not allowed"}}).encode()
        with self.assertRaises(ServerError) as error:
            await self.provider._request("poweroff")
        self.assertEqual(error.exception.status, 403)
        for output in (b"{}", b"not json", b'{"protocol":2,"ok":true}',
                       b'{"protocol":1,"ok":true,"result":[]}'):
            self.process.returncode = 0
            self.output = output
            with self.subTest(output=output), self.assertRaises(ServerError):
                await self.provider._request("status")

    async def test_timeout_reaps_helper(self):
        self.process.returncode = None
        self.process.wait.side_effect = [asyncio.TimeoutError(), 0]
        with self.assertRaisesRegex(ServerError, "timed out"):
            await self.provider._request("status")
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.wait.await_count, 2)

    async def test_timeout_cleanup_has_its_own_deadline(self):
        self.provider.timeout = 0.01
        self.process.returncode = None
        calls = 0
        async def wait():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.TimeoutError()
            await asyncio.Event().wait()
        self.process.wait.side_effect = wait
        with self.assertLogs(level="WARNING"), self.assertRaisesRegex(ServerError, "timed out"):
            await asyncio.wait_for(self.provider._request("status"), 1.0)
        self.process.kill.assert_called_once()
        _, kwargs = self.mock_spawn.call_args
        self.assertTrue(kwargs["stdout"].closed)
        self.assertTrue(kwargs["stderr"].closed)

    async def test_cancellation_reaps_process_and_propagates(self):
        self.process.returncode = None
        self.process.wait.side_effect = [asyncio.CancelledError(), 0]
        with self.assertRaises(asyncio.CancelledError):
            await self.provider._request("status")
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.wait.await_count, 2)

    async def test_missing_executable(self):
        self.mock_spawn.side_effect = FileNotFoundError("missing")
        with self.assertRaisesRegex(ServerError, "Cannot launch"):
            await self.provider._request("status")


class ProcessTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_does_not_wait_for_child_retaining_output(self):
        provider = ShepherdProvider(Config(shepherd_timeout=0.05))
        real_spawn = asyncio.create_subprocess_exec
        process = None
        with tempfile.TemporaryDirectory(prefix="helper-timeout-") as directory:
            pid_file = Path(directory) / "child.pid"
            code = ("import subprocess,sys,time; from pathlib import Path; "
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                    "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)")
            async def spawn(*args, **kwargs):
                nonlocal process
                process = await real_spawn(sys.executable, "-c", code, str(pid_file), **kwargs)
                for _ in range(200):
                    if pid_file.exists() and pid_file.read_text():
                        return process
                    await asyncio.sleep(0.01)
                self.fail("Helper fixture did not start its child")
            try:
                with patch("asyncio.create_subprocess_exec", spawn):
                    with self.assertRaisesRegex(ServerError, "timed out"):
                        await asyncio.wait_for(provider._request("status"), 3.0)
                self.assertIsNotNone(process.returncode)
                # The child's inherited output is still open when the request returns.
                os.kill(int(pid_file.read_text()), 0)
            finally:
                if pid_file.exists() and pid_file.read_text():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process is not None:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.wait_for(process.wait(), 3.0)


class ConfigurationTests(unittest.TestCase):
    def test_absolute_paths_and_positive_timeout(self):
        for name in ("shepherd_helper", "shepherd_policy", "shepherd_sudo"):
            with self.subTest(name=name), self.assertRaises(ServerError):
                ShepherdProvider(Config(**{name: "relative"}))
        with self.assertRaises(ServerError):
            ShepherdProvider(Config(shepherd_timeout=0))


if __name__ == "__main__":
    unittest.main()

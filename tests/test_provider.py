# SPDX-License-Identifier: MIT
import asyncio
import json
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
        self.process.communicate.return_value = (
            json.dumps({"protocol": 1, "ok": True, "result": {}}).encode(), b"")
        self.process.kill = unittest.mock.Mock()
        self.spawn = patch("asyncio.create_subprocess_exec", AsyncMock(return_value=self.process))
        self.mock_spawn = self.spawn.start()
        self.addCleanup(self.spawn.stop)

    async def test_argument_vector_and_noninteractive_sudo(self):
        await self.provider._request("restart", "klipper")
        args, kwargs = self.mock_spawn.call_args
        self.assertEqual(args, (self.provider.sudo, "-n", "--", self.provider.helper,
                                self.provider.policy, "restart", "klipper"))
        self.assertEqual(kwargs["stdin"], asyncio.subprocess.DEVNULL)

    async def test_denied_response_and_invalid_protocol(self):
        self.process.returncode = 1
        self.process.communicate.return_value = (json.dumps({
            "protocol": 1, "ok": False,
            "error": {"code": "denied", "message": "not allowed"}}).encode(), b"")
        with self.assertRaises(ServerError) as error:
            await self.provider._request("poweroff")
        self.assertEqual(error.exception.status, 403)
        for output in (b"{}", b"not json", b'{"protocol":2,"ok":true}',
                       b'{"protocol":1,"ok":true,"result":[]}'):
            self.process.returncode = 0
            self.process.communicate.return_value = (output, b"")
            with self.subTest(output=output), self.assertRaises(ServerError):
                await self.provider._request("status")

    async def test_timeout_reaps_helper(self):
        self.process.returncode = None
        self.process.communicate.side_effect = [asyncio.TimeoutError(), (b"", b"")]
        with self.assertRaisesRegex(ServerError, "timed out"):
            await self.provider._request("status")
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.communicate.await_count, 2)

    async def test_missing_executable(self):
        self.mock_spawn.side_effect = FileNotFoundError("missing")
        with self.assertRaisesRegex(ServerError, "Cannot launch"):
            await self.provider._request("status")


class ConfigurationTests(unittest.TestCase):
    def test_absolute_paths_and_positive_timeout(self):
        for name in ("shepherd_helper", "shepherd_policy", "shepherd_sudo"):
            with self.subTest(name=name), self.assertRaises(ServerError):
                ShepherdProvider(Config(**{name: "relative"}))
        with self.assertRaises(ServerError):
            ShepherdProvider(Config(shepherd_timeout=0))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: MIT
"""Contract checks against the actual patched Moonraker module."""
import asyncio
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from moonraker.components import machine as module
from moonraker_provider_shepherd.provider import ShepherdProvider

from support import Config, Server


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_real_module_is_the_patched_temporary_copy(self):
        self.assertTrue(Path(module.__file__).is_relative_to(os.environ["MOONRAKER_SOURCE"]))

    def test_shepherd_pid_one_and_actual_container(self):
        machine = module.Machine.__new__(module.Machine)
        with tempfile.TemporaryDirectory() as directory:
            cgroup, sched = Path(directory) / "cgroup", Path(directory) / "sched"
            sched.write_text("shepherd (1, #threads: 1)\n")
            cgroup.write_text("0::/\n")
            with patch.object(module, "CGROUP_PATH", str(cgroup)), patch.object(module, "SCHED_PATH", str(sched)):
                machine.inside_container = False
                self.assertEqual(machine._check_inside_container()["virt_type"], "none")
                cgroup.write_text("0::/docker/test\n")
                self.assertEqual(machine._check_inside_container()["virt_type"], "container")

    async def test_permission_check_delegates_to_provider(self):
        machine = module.Machine.__new__(module.Machine)
        machine.provider_type = "shepherd"
        machine.sys_provider = types.SimpleNamespace(check_sudo_access=AsyncMock(return_value=True))
        self.assertTrue(await machine.check_sudo_access())
        machine.sys_provider.check_sudo_access.assert_awaited_once()

    def test_provider_registration_in_real_machine_constructor(self):
        server = MagicMock()
        server.get_app_args.return_value = {"data_path": "/unused"}
        config = MagicMock()
        config.get_server.return_value = server
        config.get.side_effect = lambda name, default=None: "shepherd" if name == "provider" else default
        config.gettemplate.return_value = None
        config.getfloat.return_value = 60.0
        config.getchoice.return_value = "poweroff"
        with patch.object(module.Machine, "_init_allowed_services"), \
                patch.object(module.Machine, "_get_cpu_info", return_value={}), \
                patch.object(module.Machine, "_get_sdcard_info", return_value={}), \
                patch.object(module.Machine, "_check_inside_container", return_value={}), \
                patch.object(module.Machine, "_try_import_libcamera"), \
                patch.object(module, "InstallValidator"):
            machine = module.Machine(config)
        self.assertIsInstance(machine.sys_provider, ShepherdProvider)
        self.assertEqual(machine.system_info["provider"], "shepherd")


if __name__ == "__main__":
    unittest.main(verbosity=2)

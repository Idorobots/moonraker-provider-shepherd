# SPDX-License-Identifier: MIT
"""Exercise an isolated Shepherd, optionally with an actual patched Moonraker."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from support import Config, ServerError, load_provider

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SHEPHERD = os.environ.get("SHEPHERD") or shutil.which("shepherd")
MOONRAKER = os.environ.get("MOONRAKER_SOURCE")


def wait_for(function, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = function()
            if value:
                return value
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for private test service")


@unittest.skipUnless(SHEPHERD, "Set SHEPHERD to a GNU Shepherd executable")
class PrivateShepherdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="moonraker-shepherd-")
        cls.directory = Path(cls.temporary.name).resolve()
        cls.socket = cls.directory / "socket"
        cls.policy = cls.directory / "policy.scm"
        cls.policy.write_text(
            '((version . 1) (socket . ' + json.dumps(str(cls.socket)) + ') '
            '(moonraker-service . "moonraker") '
            '(services . (("klipper" start stop restart) ("moonraker" restart))) '
            '(power . ()))\n')
        cls.env = os.environ.copy()
        modules = Path(SHEPHERD).resolve().parents[1] / "share/guile/site/3.0"
        cls.env["GUILE_LOAD_PATH"] = os.pathsep.join(filter(None, (
            str(ROOT / "scheme"), str(modules), cls.env.get("GUILE_LOAD_PATH"))))
        cls.env["GUILE_AUTO_COMPILE"] = "0"
        cls.env["TEST_SLEEP"] = shutil.which("sleep")
        cls.helper = cls.directory / "helper"
        cls.helper.write_text(
            "#!/bin/sh\nexport GUILE_AUTO_COMPILE=0\nexport GUILE_LOAD_PATH="
            + shlex.quote(cls.env["GUILE_LOAD_PATH"]) + "\nexec "
            + shlex.quote(shutil.which("guile")) + " --no-auto-compile -e main -s "
            + shlex.quote(str(ROOT / "bin/moonraker-shepherd")) + ' "$@"\n')
        cls.helper.chmod(0o755)
        # Exercises the exact provider argv without requiring or granting sudo.
        cls.sudo = cls.directory / "sudo"
        cls.sudo.write_text('#!/bin/sh\n[ "$1" = -n ] && [ "$2" = -- ] || exit 99\nshift 2\nexec "$@"\n')
        cls.sudo.chmod(0o755)
        if MOONRAKER:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                cls.port = sock.getsockname()[1]
            data = cls.directory / "data"
            (data / "config").mkdir(parents=True)
            config = cls.directory / "moonraker.conf"
            config.write_text(
                f"[server]\nhost: 127.0.0.1\nport: {cls.port}\n"
                "[machine]\nprovider: shepherd\nvalidate_service: False\nvalidate_config: False\n"
                f"shepherd_helper: {cls.helper}\nshepherd_policy: {cls.policy}\n"
                f"shepherd_sudo: {cls.sudo}\n"
                "[authorization]\ntrusted_clients: 127.0.0.1/32\n")
            cls.env.update(TEST_MOONRAKER_PYTHON=sys.executable,
                           TEST_MOONRAKER_DATA=str(data), TEST_MOONRAKER_CONFIG=str(config))
        cls.log = (cls.directory / "output.log").open("w+")
        cls.daemon = subprocess.Popen([
            SHEPHERD, "--socket", str(cls.socket), "--config",
            str(ROOT / "tests/fixtures/shepherd.scm"),
            "--logfile", str(cls.directory / "shepherd.log")],
            env=cls.env, stdout=cls.log, stderr=cls.log)
        try:
            wait_for(lambda: cls.socket.exists() and cls.status().get("moonraker", {}).get("pid"))
            if MOONRAKER:
                wait_for(lambda: cls.api("/machine/system_info"))
        except Exception:
            cls.log.seek(0)
            print(cls.log.read(), file=sys.stderr)
            print(cls.command("status", check=False), file=sys.stderr)
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            # The explicit private socket is mandatory; never contact system Shepherd.
            herd = Path(SHEPHERD).resolve().with_name("herd")
            subprocess.run([str(herd), "--socket", str(cls.socket), "stop", "root"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            cls.daemon.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            cls.daemon.kill()
            cls.daemon.wait()
        cls.log.seek(0)
        output = cls.log.read()
        cls.log.close()
        # A startup failure needs its full diagnostic before temporary cleanup.
        if cls.daemon.returncode not in (0, None):
            print(output, file=sys.stderr)
        cls.temporary.cleanup()

    @classmethod
    def command(cls, *args, check=True):
        result = subprocess.run([str(cls.helper), str(cls.policy), *args],
                                env=cls.env, capture_output=True, text=True, timeout=15)
        response = json.loads(result.stdout)
        if check and (result.returncode or not response["ok"]):
            raise AssertionError(f"{response}\n{result.stderr}")
        return response

    @classmethod
    def status(cls):
        return cls.command("status")["result"]["services"]

    @classmethod
    def api(cls, endpoint, data=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{endpoint}",
            data=None if data is None else json.dumps(data).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)["result"]

    def test_inventory_and_policy_boundary(self):
        state = self.status()
        self.assertEqual(set(state), {"klipper", "moonraker"})
        self.assertEqual(state["klipper"]["active_state"], "active")
        self.assertEqual(self.command("capabilities")["result"]["power"], [])
        for args in (("stop", "unmanaged"), ("stop", "moonraker"),
                     ("poweroff",), ("reboot",), ("eval", "root")):
            with self.subTest(args=args):
                result = self.command(*args, check=False)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "denied")

    def test_policy_replacement_permissions_rejected(self):
        try:
            self.policy.chmod(0o666)
            result = self.command("capabilities", check=False)
            self.assertEqual(result["error"]["code"], "policy")
        finally:
            self.policy.chmod(0o644)

    def test_provider_stop_start_and_self_restart(self):
        asyncio.run(self.provider_stop_start_and_self_restart())

    async def provider_stop_start_and_self_restart(self):
        provider = load_provider()(Config(shepherd_helper=str(self.helper),
                                          shepherd_policy=str(self.policy),
                                          shepherd_sudo=str(self.sudo)))
        await provider.initialize()
        old = self.status()["moonraker"]["pid"]
        await provider.do_service_action("stop", "klipper")
        self.assertEqual(self.status()["klipper"]["active_state"], "inactive")
        self.assertEqual(self.status()["moonraker"]["pid"], old)
        await provider.do_service_action("start", "klipper")
        await provider.do_service_action("restart", "moonraker")
        wait_for(lambda: self.status()["moonraker"]["pid"] != old)
        self.assertTrue(await provider.check_sudo_access())
        if MOONRAKER:
            wait_for(lambda: self.api("/machine/system_info"))

    @unittest.skipUnless(MOONRAKER, "Run tests/check_moonraker.py for HTTP compatibility checks")
    def test_actual_moonraker_api_and_self_restart(self):
        info = self.api("/machine/system_info")["system_info"]
        self.assertEqual(info["provider"], "shepherd")
        self.assertEqual(set(info["available_services"]), {"klipper", "moonraker"})
        old = self.status()["moonraker"]["pid"]
        self.assertEqual(self.api("/machine/services/stop", {"service": "klipper"}), "ok")
        self.assertEqual(self.status()["klipper"]["active_state"], "inactive")
        self.assertEqual(self.api("/machine/system_info")["system_info"]["provider"], "shepherd")
        self.assertEqual(self.api("/machine/services/start", {"service": "klipper"}), "ok")
        self.assertEqual(self.api("/machine/services/restart", {"service": "moonraker"}), "ok")
        wait_for(lambda: self.status()["moonraker"]["pid"] != old)
        wait_for(lambda: self.api("/machine/system_info"))
        self.assertTrue(self.api("/machine/sudo/info?check_access=true")["sudo_access"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

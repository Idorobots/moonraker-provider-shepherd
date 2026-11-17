# SPDX-License-Identifier: MIT
"""Moonraker's machine-provider interface backed by a bounded Guile helper."""

import asyncio
import json
import logging
from pathlib import Path
import tempfile

from moonraker.components.machine import BaseProvider


class ShepherdProvider(BaseProvider):
    def __init__(self, config):
        super().__init__(config)
        self.helper = config.get("shepherd_helper", "/run/current-system/profile/bin/moonraker-shepherd")
        self.policy = config.get("shepherd_policy", "/etc/moonraker/shepherd-policy.scm")
        self.sudo = config.get("shepherd_sudo", "/run/privileged/bin/sudo")
        for name, value in (("shepherd_helper", self.helper),
                            ("shepherd_policy", self.policy),
                            ("shepherd_sudo", self.sudo)):
            if not Path(value).is_absolute():
                raise config.error(f"{name} must be an absolute path")
        self.timeout = config.getfloat("shepherd_timeout", 60.0, above=0.0)
        self.details = {}
        self.capabilities = {}
        self._poll_lock = asyncio.Lock()
        self._unavailable = False

    async def _request(self, action, service=None):
        args = [self.sudo, "-n", "--", self.helper, self.policy, action]
        if service is not None:
            args.append(service)
        # A child retaining an output pipe can keep communicate()/wait() blocked
        # after sudo exits. Regular files let us reap the immediate process
        # independently, including when a privileged helper outlives its client.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                process = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.DEVNULL,
                    stdout=output, stderr=errors)
            except OSError as exc:
                raise self.server.error(f"Cannot launch Shepherd helper: {exc}", 503) from exc
            try:
                await asyncio.wait_for(process.wait(), self.timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                # Shepherd may still finish an already-dispatched action.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), min(self.timeout, 1.0))
                except asyncio.TimeoutError:
                    logging.warning("Shepherd helper did not exit during bounded cleanup")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise self.server.error("Shepherd helper timed out; check service status", 503) from exc
            output.seek(0)
            errors.seek(0)
            stdout, stderr = output.read(), errors.read()
        try:
            response = json.loads(stdout)
            if not isinstance(response, dict) or response.get("protocol") != 1:
                raise ValueError("unsupported helper protocol")
            if not isinstance(response.get("ok"), bool):
                raise ValueError("missing helper outcome")
        except (ValueError, UnicodeError) as exc:
            detail = stderr.decode(errors="replace").strip()[-1000:]
            raise self.server.error(f"Invalid Shepherd helper response: {detail or exc}", 503) from exc
        if process.returncode or not response["ok"]:
            error = response.get("error", {})
            if not isinstance(error, dict):
                error = {}
            code = 403 if error.get("code") == "denied" else 503
            raise self.server.error(f"Shepherd: {error.get('message', 'helper failed')}", code)
        result = response.get("result")
        if not isinstance(result, dict):
            raise self.server.error("Invalid Shepherd helper result", 503)
        return result

    async def initialize(self):
        try:
            self.capabilities = await self._request("capabilities")
            if self.shutdown_action not in self.capabilities.get("power", []):
                logging.info("Shepherd shutdown action %s is not permitted", self.shutdown_action)
            await self._refresh(notify=False)
        except Exception as exc:
            self.server.add_warning(f"Shepherd management unavailable: {exc}")
        # Keep polling after an initial failure so management can recover.
        stats = self.server.lookup_component("proc_stats")
        stats.register_stat_callback(self._update_service_status)

    async def check_sudo_access(self):
        try:
            self.capabilities = await self._request("capabilities")
            return True
        except Exception:
            return False

    async def _refresh(self, notify=True):
        result = await self._request("status")
        details = result.get("services")
        if not isinstance(details, dict):
            raise self.server.error("Invalid Shepherd service inventory", 503)
        states = {}
        for name, info in details.items():
            if (not isinstance(name, str) or not isinstance(info, dict)
                    or not isinstance(info.get("active_state"), str)
                    or not isinstance(info.get("sub_state"), str)
                    or not isinstance(info.get("actions"), list)):
                raise self.server.error("Invalid Shepherd service state", 503)
            states[name] = {key: info[key] for key in ("active_state", "sub_state")}
        # The privileged policy is authoritative, including for additional services.
        old_states = dict(self.available_services)
        self.details = details
        self.available_services.clear()
        self.available_services.update(states)
        if notify:
            for name in old_states.keys() | states.keys():
                state = states.get(name, {"active_state": "unknown", "sub_state": "not-found"})
                if old_states.get(name) != state:
                    self.server.send_event("machine:service_state_changed", {name: state})
        machine = self.server.lookup_component("machine")
        machine.system_info["available_services"] = list(states)
        machine.system_info["service_state"] = self.available_services
        if self._unavailable:
            logging.info("Shepherd management connection recovered")
        self._unavailable = False

    async def _update_service_status(self, sequence):
        if sequence % 2 or self._poll_lock.locked():
            return
        async with self._poll_lock:
            try:
                await self._refresh()
            except Exception:
                if not self._unavailable:
                    logging.exception("Shepherd service status unavailable")
                self._unavailable = True
                for name in list(self.available_services):
                    state = {"active_state": "unknown", "sub_state": "unavailable"}
                    if self.available_services[name] != state:
                        self.available_services[name] = state
                        self.server.send_event("machine:service_state_changed", {name: state})

    async def do_service_action(self, action, service_name):
        if action not in ("start", "stop", "restart"):
            raise self.server.error(f"Unsupported Shepherd action: {action}", 400)
        # Fetch a current inventory; do not trust caller input or stale permissions.
        await self._refresh()
        info = self.details.get(service_name, {})
        if action not in info.get("actions", []):
            raise self.server.error(f"Shepherd action not permitted: {action} {service_name}", 403)
        result = await self._request(action, service_name)
        if not result.get("accepted"):
            await self._refresh()

    async def shutdown(self):
        await self._request(self.shutdown_action)

    async def reboot(self):
        await self._request("reboot")

    async def check_virt_status(self):
        # Preserve Moonraker's detection; Shepherd does not identify hypervisors.
        machine = self.server.lookup_component("machine")
        return dict(machine.system_info["virtualization"])

    async def extract_service_info(self, service_name, pid, properties=None, raw=False):
        try:
            await self._refresh()
        except Exception:
            logging.exception("Cannot identify Shepherd service for %s", service_name)
            return {}
        for name, info in self.details.items():
            if info.get("pid") == pid:
                props = info.get("properties", {})
                if properties is not None:
                    props = {key: value for key, value in props.items() if key in properties}
                return {"unit_name": name, "is_default": name == service_name,
                        "manager": "shepherd", "properties": props}
        return {}

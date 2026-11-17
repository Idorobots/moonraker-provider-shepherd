# SPDX-License-Identifier: MIT
"""Small test doubles; no Moonraker dependency for the provider unit tests."""
import importlib
import sys
import types
from unittest.mock import patch


class ServerError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Server:
    error = ServerError

    def __init__(self):
        self.events = []
        self.warnings = []
        self.callbacks = []
        self.components = {
            "machine": types.SimpleNamespace(system_info={"virtualization": {
                "virt_type": "none", "virt_identifier": "none"}}),
            "proc_stats": types.SimpleNamespace(register_stat_callback=self.callbacks.append)}

    def lookup_component(self, name):
        return self.components[name]

    def send_event(self, name, value):
        self.events.append((name, value))

    def add_warning(self, message):
        self.warnings.append(message)


class Config:
    error = ServerError

    def __init__(self, server=None, **options):
        self.server = server or Server()
        self.options = options

    def get_server(self):
        return self.server

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getfloat(self, name, default, above):
        value = float(self.get(name, default))
        if value <= above:
            raise self.error("Invalid positive timeout")
        return value


def load_provider():
    class BaseProvider:
        def __init__(self, config):
            self.server = config.get_server()
            self.shutdown_action = config.get("shutdown_action", "poweroff")
            self.available_services = {}

    machine = types.ModuleType("moonraker.components.machine")
    machine.BaseProvider = BaseProvider
    with patch.dict(sys.modules, {"moonraker": types.ModuleType("moonraker"),
                                "moonraker.components": types.ModuleType("moonraker.components"),
                                "moonraker.components.machine": machine}):
        return importlib.import_module("moonraker_provider_shepherd.provider").ShepherdProvider


def inventory(state="active", pid=42):
    return {"services": {"klipper": {
        "active_state": state, "sub_state": "running" if state == "active" else "dead",
        "pid": pid, "actions": ["start", "stop", "restart"],
        "properties": {"Description": "Printer", "Requires": []}}}}

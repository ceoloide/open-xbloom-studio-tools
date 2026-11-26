"""In-memory stand-in for the parts of ``bleak`` the connector uses."""

from __future__ import annotations

import types
from unittest import mock


class Services:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def get_characteristic(self, uuid):
        return None if uuid in self.missing else object()


class Client:
    instances: list[Client] = []
    drops_while_connecting = 0
    leaves_connected = True

    def __init__(self, device, *, timeout, disconnected_callback):
        self.device = device
        self.timeout = timeout
        self.disconnected_callback = disconnected_callback
        self.services = Services(Client.missing)
        self.is_connected = False
        self.writes = []
        self.notify = None
        self.calls = []
        self.values = {}
        Client.instances.append(self)

    async def connect(self):
        self.calls.append("connect")
        for _ in range(Client.drops_while_connecting):
            self.disconnected_callback(self)
        self.is_connected = Client.leaves_connected

    async def disconnect(self):
        self.calls.append("disconnect")
        if self.is_connected:
            self.is_connected = False
            self.disconnected_callback(self)

    async def start_notify(self, uuid, callback):
        self.calls.append(("start_notify", uuid))
        self.notify = callback

    async def stop_notify(self, uuid):
        self.calls.append(("stop_notify", uuid))
        self.notify = None

    async def write_gatt_char(self, uuid, data, response):
        self.writes.append((uuid, bytes(data), response))

    async def read_gatt_char(self, uuid):
        return bytearray(self.values[uuid])


Client.missing = ()


class Device:
    def __init__(self, address, name=None):
        self.address = address
        self.name = name


class Advertisement:
    def __init__(self, local_name=None, service_uuids=(), rssi=-50):
        self.local_name = local_name
        self.service_uuids = list(service_uuids)
        self.rssi = rssi


def module(*, found=None, advertised=None):
    scanner = mock.Mock()

    async def find_device_by_address(address, timeout):
        scanner.find_calls.append((address, timeout))
        return found

    async def discover(timeout, return_adv):
        scanner.discover_calls.append((timeout, return_adv))
        return advertised or {}

    scanner.find_calls = []
    scanner.discover_calls = []
    scanner.find_device_by_address = find_device_by_address
    scanner.discover = discover
    Client.instances = []
    Client.missing = ()
    Client.drops_while_connecting = 0
    Client.leaves_connected = True
    return types.SimpleNamespace(BleakClient=Client, BleakScanner=scanner)

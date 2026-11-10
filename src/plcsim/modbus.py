"""Expose a simulated PLC over Modbus TCP, so real SCADA/HMI tools can talk to it.

Written by hand, without a Modbus library, because the protocol is small and seeing it
is the point. A request is a 7-byte MBAP header followed by a PDU:

    transaction id (2) | protocol id = 0 (2) | length (2) | unit id (1) | function (1) | data...

Register map (the same idea as a real PLC's "Modbus mapping" table):

    discrete inputs   0..   boolean I tags        read-only (FC2)
                    100..   boolean M tags        read-only (FC2)
    coils             0..   Q tags                read-only (FC1): only the program drives outputs
                    100..   CMD tags              writable (FC5/FC15): writing 1 presses the button
    input registers   0..   analog I tags x10     read-only (FC4)
                    100..   analog M tags x10     read-only (FC4)
                    200     mode (1 = RUN)        201 alarms not acknowledged
    holding registers 0..   SP tags x10           writable (FC6/FC16), within the setpoint limits

Modbus has no authentication and no encryption. That is why this server binds to
127.0.0.1 by default and refuses other addresses unless asked explicitly: in a plant,
Modbus lives on an isolated OT network behind a firewall (IEC 62443 zones and conduits).
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import Any

from plcsim.runtime import PLC, WriteRejected

SCALE = 10
ILLEGAL_FUNCTION, ILLEGAL_ADDRESS, ILLEGAL_VALUE = 1, 2, 3


@dataclass(frozen=True)
class Entry:
    table: str  # "coil", "discrete", "input", "holding"
    address: int
    tag: str
    writable: bool = False


def register_map(plc: PLC) -> list[Entry]:
    entries: list[Entry] = []

    def add(table: str, start: int, names: list[str], writable: bool = False) -> None:
        entries.extend(Entry(table, start + i, name, writable) for i, name in enumerate(names))

    tags = plc.program.tags
    add("discrete", 0, [t.name for t in tags if t.area == "I" and t.kind is bool])
    add("discrete", 100, [t.name for t in tags if t.area == "M" and t.kind is bool])
    add("coil", 0, [t.name for t in tags if t.area == "Q"])
    add("coil", 100, [t.name for t in tags if t.area == "CMD"], writable=True)
    add("input", 0, [t.name for t in tags if t.area == "I" and t.kind is float])
    add("input", 100, [t.name for t in tags if t.area == "M" and t.kind is float])
    add("holding", 0, [t.name for t in tags if t.area == "SP"], writable=True)
    return entries


def describe_map(plc: PLC) -> str:
    rows = [f"{'table':9} {'addr':>5}  {'tag':14} access  description"]
    for entry in register_map(plc):
        tag = plc.tags[entry.tag]
        scale = " (x10)" if entry.table in ("input", "holding") else ""
        rows.append(f"{entry.table:9} {entry.address:5}  {entry.tag:14} {'RW' if entry.writable else 'R ':6}  {tag.description}{scale}")
    rows.append(f"{'input':9} {200:5}  {'MODE':14} R       1 = RUN, 0 = STOP")
    rows.append(f"{'input':9} {201:5}  {'ALARMS':14} R       alarms not yet acknowledged")
    return "\n".join(rows)


class ModbusServer:
    def __init__(self, plc: PLC, host: str = "127.0.0.1", port: int = 5020, allow_remote: bool = False) -> None:
        if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
            raise RuntimeError(
                f"Refusing to serve Modbus on {host}: Modbus has no authentication, so anyone who can reach "
                "the port can press buttons. Pass allow_remote=True (--allow-remote) only on an isolated network."
            )
        self.plc, self.host, self.port = plc, host, port
        entries = register_map(plc)
        self._by_table: dict[str, dict[int, Entry]] = {}
        for entry in entries:
            self._by_table.setdefault(entry.table, {})[entry.address] = entry
        self.server: asyncio.base_events.Server | None = None
        self.requests = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(7)
                transaction, protocol, length, unit = struct.unpack(">HHHB", header)
                if protocol != 0 or not 2 <= length <= 254:
                    break
                pdu = await reader.readexactly(length - 1)
                reply = self.handle(pdu)
                writer.write(struct.pack(">HHHB", transaction, 0, len(reply) + 1, unit) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    # ---- PDU handling (pure function of the PLC state, easy to test) -------
    def handle(self, pdu: bytes) -> bytes:
        self.requests += 1
        function = pdu[0]
        try:
            if function in (1, 2, 3, 4):
                address, count = struct.unpack(">HH", pdu[1:5])
                return self._read(function, address, count)
            if function == 5:
                address, value = struct.unpack(">HH", pdu[1:5])
                if value not in (0x0000, 0xFF00):
                    return _error(function, ILLEGAL_VALUE)
                self._write_coils(address, [value == 0xFF00])
                return pdu[:5]
            if function == 6:
                address, value = struct.unpack(">HH", pdu[1:5])
                self._write_registers(address, [value])
                return pdu[:5]
            if function == 15:
                address, count, _ = struct.unpack(">HHB", pdu[1:6])
                bits = [bool(pdu[6 + i // 8] >> (i % 8) & 1) for i in range(count)]
                self._write_coils(address, bits)
                return pdu[:5]
            if function == 16:
                address, count, _ = struct.unpack(">HHB", pdu[1:6])
                values = list(struct.unpack(f">{count}H", pdu[6 : 6 + 2 * count]))
                self._write_registers(address, values)
                return pdu[:5]
            return _error(function, ILLEGAL_FUNCTION)
        except _ModbusError as exc:
            return _error(function, exc.code)
        except (struct.error, IndexError):
            return _error(function, ILLEGAL_VALUE)

    def _read(self, function: int, address: int, count: int) -> bytes:
        if not 1 <= count <= 125:
            raise _ModbusError(ILLEGAL_VALUE)
        values = [self._value(function, address + i) for i in range(count)]
        if function in (1, 2):
            packed = bytearray((count + 7) // 8)
            for i, bit in enumerate(values):
                if bit:
                    packed[i // 8] |= 1 << (i % 8)
            return bytes([function, len(packed)]) + bytes(packed)
        return bytes([function, 2 * count]) + struct.pack(f">{count}H", *values)

    def _value(self, function: int, address: int) -> int:
        if function == 4 and address == 200:
            return 1 if self.plc.mode == "RUN" else 0
        if function == 4 and address == 201:
            return sum(1 for a in self.plc.alarms.values() if not a.acknowledged)
        table = {1: "coil", 2: "discrete", 3: "holding", 4: "input"}[function]
        entry = self._by_table.get(table, {}).get(address)
        if entry is None:
            raise _ModbusError(ILLEGAL_ADDRESS)
        value: Any = self.plc.read(entry.tag)
        if function in (1, 2):
            return int(bool(value))
        return round(float(value) * SCALE) & 0xFFFF

    def _write_coils(self, address: int, bits: list[bool]) -> None:
        entries = [self._writable("coil", address + i) for i in range(len(bits))]
        for entry, bit in zip(entries, bits):
            if bit:
                self.plc.command(entry.tag, who="modbus")

    def _write_registers(self, address: int, values: list[int]) -> None:
        entries = [self._writable("holding", address + i) for i in range(len(values))]
        for entry, raw in zip(entries, values):
            signed = raw - 0x10000 if raw >= 0x8000 else raw
            try:
                self.plc.write_setpoint(entry.tag, signed / SCALE, who="modbus")
            except WriteRejected:
                raise _ModbusError(ILLEGAL_VALUE) from None

    def _writable(self, table: str, address: int) -> Entry:
        entry = self._by_table.get(table, {}).get(address)
        if entry is None or not entry.writable:
            raise _ModbusError(ILLEGAL_ADDRESS)
        return entry


class _ModbusError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def _error(function: int, code: int) -> bytes:
    return bytes([function | 0x80, code])


async def run_realtime(plc: PLC, speed: float = 1.0, stop: asyncio.Event | None = None) -> None:
    """Scan forever at the PLC's scan time. speed=10 runs the process ten times faster."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    next_scan = loop.time()
    while not stop.is_set():
        plc.scan()
        next_scan += plc.scan_time / speed
        await asyncio.sleep(max(0.0, next_scan - loop.time()))

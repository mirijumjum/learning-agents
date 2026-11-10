"""Events out of the PLC: publish/subscribe, report by exception, webhooks.

Modbus is polling: the SCADA asks "what is the level?" every second, whether it changed
or not. Modern plants also push events: the PLC (or an edge gateway next to it)
publishes a message only when something changes, and anyone interested subscribes.
That is how MQTT, MQTT Sparkplug B and OPC UA subscriptions work.

Topics follow the MQTT convention, with + matching one level and # the rest:

    plcsim/<program>/status                  online/offline (birth and death certificates)
    plcsim/<program>/tag/<NAME>              a value changed (booleans: any change;
                                             analog: only beyond the deadband)
    plcsim/<program>/alarm/<NAME>            raised, cleared, acknowledged
    plcsim/<program>/audit/<kind>            setpoint, command, force, mode: who changed what

    plcsim/tank/alarm/#      every alarm of the tank
    plcsim/+/tag/LEVEL       LEVEL of every program

Sinks deliver events somewhere: the console, a JSONL file, or an HTTP webhook signed
with HMAC-SHA256 (the same scheme GitHub and GitLab use), with retries. Swapping the
in-process bus for a real MQTT broker only changes the sink; the topics stay the same.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
import time as _time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from plcsim.runtime import PLC


@dataclass(frozen=True)
class Event:
    topic: str
    kind: str
    t: float
    seq: int
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


def topic_matches(pattern: str, topic: str) -> bool:
    want, have = pattern.split("/"), topic.split("/")
    for i, part in enumerate(want):
        if part == "#":
            return True
        if i >= len(have) or part not in ("+", have[i]):
            return False
    return len(want) == len(have)


class EventBus:
    """In-process publish/subscribe. A slow or failing subscriber cannot stop the others."""

    def __init__(self) -> None:
        self._subscribers: list[tuple[str, Callable[[Event], None]]] = []
        self.errors: list[str] = []

    def subscribe(self, pattern: str, callback: Callable[[Event], None]) -> Callable[[], None]:
        entry = (pattern, callback)
        self._subscribers.append(entry)
        return lambda: self._subscribers.remove(entry) if entry in self._subscribers else None

    def publish(self, event: Event) -> None:
        for pattern, callback in list(self._subscribers):
            if topic_matches(pattern, event.topic):
                try:
                    callback(event)
                except Exception as exc:  # noqa: BLE001 - one bad subscriber must not stop the PLC
                    self.errors.append(f"{pattern}: {exc}")


@dataclass
class EventPublisher:
    """Watches a PLC after every scan and publishes what changed.

    deadband: analog values are reported only when they move by at least this much
    since the last report ("report by exception"), so a noisy sensor doesn't flood
    the network. heartbeat: seconds between status messages, so subscribers can
    tell a quiet plant from a dead link.
    """

    bus: EventBus
    prefix: str = "plcsim"
    deadband: float = 1.0
    heartbeat: float = 30.0
    seq: int = 0
    _last: dict[str, Any] = field(default_factory=dict)
    _event_index: int = 0
    _last_heartbeat: float = 0.0

    def attach(self, plc: PLC) -> "EventPublisher":
        self.base = f"{self.prefix}/{plc.program.name}"
        plc.after_scan.append(self)
        self._event_index = len(plc.events)
        self._last = self._values(plc)
        self._emit(plc, "status", "birth", {"state": "online", "values": dict(self._last)})
        self._last_heartbeat = plc.time
        return self

    def detach(self, plc: PLC) -> None:
        if self in plc.after_scan:
            plc.after_scan.remove(self)
        self._emit(plc, "status", "death", {"state": "offline"})

    @staticmethod
    def _values(plc: PLC) -> dict[str, Any]:
        return {**plc.I, **plc.Q, **plc.M, **plc.SP}

    def _emit(self, plc: PLC, subtopic: str, kind: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        self.bus.publish(Event(f"{self.base}/{subtopic}", kind, round(plc.time, 2), self.seq, payload))

    def __call__(self, plc: PLC) -> None:
        for name, value in self._values(plc).items():
            old = self._last.get(name)
            if isinstance(value, bool) or old is None or isinstance(old, bool):
                changed = value != old
            else:
                changed = abs(float(value) - float(old)) >= self.deadband
            if changed:
                tag = plc.tags[name]
                self._emit(plc, f"tag/{name}", "change", {"name": name, "value": value, "previous": old, "unit": tag.unit, "area": tag.area})
                self._last[name] = value
        for record in plc.events[self._event_index:]:
            kind = record["event"]
            details = {k: v for k, v in record.items() if k not in ("t", "event")}
            if kind == "alarm":
                alarm = plc.alarms.get(record["name"])
                self._emit(plc, f"alarm/{record['name']}", "raised", {**details, "severity": alarm.severity if alarm else "high"})
            elif kind == "alarm_cleared":
                self._emit(plc, f"alarm/{record['name']}", "cleared", details)
            elif kind == "acknowledge":
                for name in record.get("alarms", []):
                    self._emit(plc, f"alarm/{name}", "acknowledged", {"who": record.get("who")})
            else:
                self._emit(plc, f"audit/{kind}", kind, details)
        self._event_index = len(plc.events)
        if self.heartbeat and plc.time - self._last_heartbeat >= self.heartbeat:
            self._last_heartbeat = plc.time
            self._emit(plc, "status", "heartbeat", {"state": "online", "scans": plc.scans, "alarms": len(plc.alarms)})


# ------------------------------------------------------------------ sinks
class ConsoleSink:
    def __init__(self, show_tags: bool = True) -> None:
        self.show_tags = show_tags

    def __call__(self, event: Event) -> None:
        if not self.show_tags and "/tag/" in event.topic:
            return
        payload = event.payload
        if event.kind == "change":
            text = f"{payload['name']} {payload['previous']} -> {payload['value']} {payload['unit']}".strip()
        else:
            text = json.dumps({k: v for k, v in payload.items() if k != "values"}, ensure_ascii=False)
        print(f"[{event.t:7.1f}s] #{event.seq:<4} {event.topic:38} {event.kind:12} {text}")


class JsonlSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")

    def __call__(self, event: Event) -> None:
        self._file.write(event.to_json() + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, secret: str, signature: str) -> bool:
    return hmac.compare_digest(sign(body, secret), signature or "")


class WebhookSink:
    """POSTs each event as JSON. A background thread sends, so a slow receiver never delays a scan.

    Headers: X-PLCSim-Event (topic), X-PLCSim-Delivery (sequence number, for de-duplication),
    X-PLCSim-Signature (HMAC-SHA256 of the body with the shared secret).
    Failed deliveries are retried with exponential backoff, then counted as dropped.
    """

    def __init__(self, url: str, secret: str, retries: int = 3, timeout: float = 5.0, backoff: float = 0.5, max_queue: int = 10_000) -> None:
        if not secret:
            raise ValueError("a webhook needs a shared secret, so the receiver can tell real events from forged ones")
        self.url, self.secret, self.retries, self.timeout, self.backoff = url, secret, retries, timeout, backoff
        self.queue: queue.Queue[Event | None] = queue.Queue(max_queue)
        self.sent = self.failed = self.dropped = 0
        self._worker = threading.Thread(target=self._run, name="webhook-sink", daemon=True)
        self._worker.start()

    def __call__(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            if event is None:
                return
            body = event.to_json().encode()
            request = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Content-Type": "application/json", "X-PLCSim-Event": event.topic,
                "X-PLCSim-Delivery": str(event.seq), "X-PLCSim-Signature": sign(body, self.secret),
            })
            for attempt in range(self.retries + 1):
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        if 200 <= response.status < 300:
                            self.sent += 1
                            break
                except (urllib.error.URLError, OSError):
                    pass
                if attempt == self.retries:
                    self.failed += 1
                else:
                    _time.sleep(self.backoff * 2**attempt)

    def close(self, timeout: float = 10.0) -> None:
        """Deliver what is queued, then stop."""
        self.queue.put(None)
        self._worker.join(timeout)


# ------------------------------------------------------------------ receiver
def create_receiver(secret: str, on_event: Callable[[dict[str, Any]], None]):
    """An aiohttp app that accepts signed webhooks: what a MES, historian or ticketing tool would run."""
    from aiohttp import web

    seen: set[str] = set()

    async def receive(request: web.Request) -> web.Response:
        body = await request.read()
        if not verify(body, secret, request.headers.get("X-PLCSim-Signature", "")):
            return web.json_response({"error": "bad signature"}, status=401)
        delivery = request.headers.get("X-PLCSim-Delivery", "")
        if delivery in seen:
            return web.json_response({"status": "duplicate"})
        seen.add(delivery)
        on_event(json.loads(body))
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_post("/events", receive)
    return app


@dataclass
class AlarmInbox:
    """Collects raised alarms so an agent can explain them after the scan has finished."""

    raised: list[Event] = field(default_factory=list)

    def __call__(self, event: Event) -> None:
        if event.kind == "raised":
            self.raised.append(event)

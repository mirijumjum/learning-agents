"""plcsim: function blocks, the rung language, the scan cycle, the example programs,
signals, events, Modbus and the agents (on the scripted model)."""

import asyncio
import json
import random
import socket
import struct

import pytest

from plcsim.agent import MOTOR_CHECKS, LadderCoder, OperatorAgent, OperatorTools, explain_alarms, predict, run_checks
from plcsim.blocks import CTU, RS, SR, TOF, TON, TP, R_TRIG
from plcsim.cli import main as cli
from plcsim.events import AlarmInbox, EventBus, EventPublisher, WebhookSink, create_receiver, sign, topic_matches
from plcsim.ladder import LadderError, compile_program
from plcsim.modbus import ModbusServer, register_map
from plcsim.programs import MOTOR_RUNGS, MOTOR_TAGS, LadderProgram, build
from plcsim.runtime import PLC, Tag, WriteRejected
from plcsim.signals import Stimulus, ma_to_percent, namur_status, parse_signal, percent_to_ma


def pulse(plc, name):
    plc.set_input(name, True)
    plc.scan()
    plc.set_input(name, False)


# ------------------------------------------------------------------ blocks
def test_ton_needs_the_input_held_for_the_full_time():
    timer = TON(1.0)
    for _ in range(9):
        assert not timer(True, 0.1)
    assert timer(True, 0.1)
    assert not timer(False, 0.1) and timer.ET == 0


def test_tof_holds_after_the_input_drops():
    timer = TOF(0.5)
    timer(True, 0.1)
    assert all(timer(False, 0.1) for _ in range(4))
    assert not timer(False, 0.1)


def test_tp_gives_a_fixed_pulse_whatever_the_input_does():
    timer = TP(0.3)
    assert timer(True, 0.1) and timer(False, 0.1) and timer(False, 0.1)
    assert not timer(False, 0.1)


def test_counter_counts_edges_not_scans():
    counter = CTU(3)
    for level in [1, 1, 1, 0, 1, 0, 1]:
        counter(bool(level))
    assert counter.CV == 3 and counter.Q
    counter(False, R=True)
    assert counter.CV == 0


def test_edges_and_bistables():
    edge = R_TRIG()
    assert [edge(x) for x in (False, True, True, False, True)] == [False, True, False, False, True]
    assert RS()(S=True, R1=True) is False  # reset wins: the safe choice
    assert SR()(S1=True, R=True) is True


# ------------------------------------------------------------------ rung language
TAGS = {"A": "I", "B": "I", "LEVEL": "I", "OUT": "Q", "MEM": "M", "SP": "SP"}


@pytest.mark.parametrize("source, message", [
    ("A := B", "rungs may only write outputs"),
    ("OUT := NOPE", "unknown tag"),
    ("TON T1(IN := A)", "needs PT"),
    ("TON T1(IN := A, PT := T#1s)\nTON T1(IN := B, PT := T#1s)", "declared twice"),
    ("OUT := A AND", "expected a value"),
    ("OUT := X.Q", "not a function block"),
    ("TON T1(FOO := A, PT := T#1s)", "has no input FOO"),
    ("", "no rungs"),
])
def test_bad_programs_are_rejected_before_they_run(source, message):
    with pytest.raises(LadderError, match=message):
        compile_program(source, TAGS)


def test_rungs_can_read_a_timer_declared_further_down():
    compiled = compile_program("OUT := T1.Q  # comment\nTON T1(IN := A, PT := T#200ms)", TAGS)
    assert compiled.block_types == {"T1": "TON"}


def test_comparisons_arithmetic_and_set_reset():
    tags = [Tag("LEVEL", "I", float), Tag("A", "I"), Tag("OUT", "Q"), Tag("MEM", "M")]
    program = LadderProgram("t", "OUT := LEVEL * 2 > 50 AND NOT A\nSET MEM IF LEVEL >= 90\nRESET MEM IF A", tags)
    plc = PLC(program)
    plc.set_input("LEVEL", 30.0)
    plc.scan()
    assert plc.Q["OUT"] and not plc.M["MEM"]
    plc.set_input("LEVEL", 95.0)
    plc.scan()
    plc.set_input("LEVEL", 10.0)
    plc.scan()
    assert plc.M["MEM"]  # latched
    pulse(plc, "A")
    assert not plc.M["MEM"]


# ------------------------------------------------------------------ scan cycle
def test_the_program_sees_inputs_only_at_the_start_of_a_scan():
    plc = build("motor").plc
    plc.set_input("START", True)
    assert not plc.Q["MOTOR"]  # nothing happens between scans
    plc.scan()
    assert plc.Q["MOTOR"]


def test_stop_mode_drops_every_output():
    plc = build("motor").plc
    pulse(plc, "START")
    plc.set_mode("STOP")
    plc.scan()
    assert not any(plc.Q.values())


def test_forcing_overrides_the_program_and_is_logged():
    plc = build("motor").plc
    plc.force("MOTOR", True)
    plc.scan()
    assert plc.Q["MOTOR"] and plc.events[-1]["event"] == "force"
    with pytest.raises(WriteRejected):
        plc.force("FAULT", True)


def test_setpoints_respect_limits_and_each_other():
    plc = build("tank").plc
    with pytest.raises(WriteRejected, match="between"):
        plc.write_setpoint("SP_HIGH", 99)
    with pytest.raises(WriteRejected, match="at least 10"):
        plc.write_setpoint("SP_LOW", 65)
    with pytest.raises(WriteRejected, match="not a setpoint"):
        plc.write_setpoint("LEVEL", 10)
    plc.write_setpoint("SP_HIGH", 80, who="test")
    assert plc.SP["SP_HIGH"] == 80 and plc.events[-1] == {"t": 0.0, "event": "setpoint", "who": "test", "name": "SP_HIGH", "old": 70.0, "new": 80.0}


def test_commands_are_true_for_exactly_one_scan():
    plc = build("tank").plc
    plc.command("STOP_AUTO")
    plc.scan()
    assert plc.CMD["STOP_AUTO"] and not plc.M["AUTO"]
    plc.scan()
    assert not plc.CMD["STOP_AUTO"]


def test_alarm_stays_listed_until_cleared_and_acknowledged():
    plc = build("tank").plc
    plc.set_input("ESTOP_OK", False)
    plc.scan()
    assert plc.alarms["ESTOP"].active
    plc.set_input("ESTOP_OK", True)
    plc.scan()
    assert "ESTOP" in plc.alarms and not plc.alarms["ESTOP"].active  # cleared, but nobody saw it yet
    plc.acknowledge()
    assert "ESTOP" not in plc.alarms


# ------------------------------------------------------------------ example programs
def test_reference_motor_rungs_pass_every_check():
    assert run_checks(MOTOR_RUNGS, MOTOR_TAGS, MOTOR_CHECKS) == []


def test_tank_cycles_between_the_setpoints():
    scenario = build("tank")
    plc = scenario.plc
    levels = []
    for _ in range(300):
        plc.run_for(1)
        levels.append(plc.I["LEVEL"])
    assert 29 <= min(levels[60:]) <= 31 and 69 <= max(levels[60:]) <= 71
    assert not plc.alarms


@pytest.mark.parametrize("trip", ["ESTOP_OK", "LSHH"])
def test_tank_interlocks_beat_the_control_logic(trip):
    scenario = build("tank")
    plc = scenario.plc
    plc.run_for(30)  # below SP_LOW by now: the pump is filling
    assert plc.Q["PUMP"]
    plc.force(trip, trip == "LSHH")
    plc.scan()
    assert not plc.Q["PUMP"]


def test_tank_pump_failure_latches_until_reset():
    scenario = build("tank")
    scenario.actors["process"].pump_broken = True
    plc = scenario.plc
    plc.run_for(30)
    assert plc.M["PUMP_FAULT"] and "PUMP_FAULT" in plc.alarms and plc.Q["BEACON"]
    scenario.actors["process"].pump_broken = False
    plc.command("RESET_FAULT")
    plc.run_for(1)
    assert not plc.M["PUMP_FAULT"]


def test_traffic_light_never_shows_green_to_cars_and_pedestrians_together():
    plc = build("traffic_light").plc
    rng = random.Random(7)
    walks = 0
    for _ in range(3000):
        plc.set_input("PED_BUTTON", rng.random() < 0.01)
        plc.scan()
        lights = plc.Q
        assert sum((lights["CAR_RED"], lights["CAR_AMBER"], lights["CAR_GREEN"])) == 1
        assert not (lights["WALK"] and not lights["CAR_RED"])
        walks += lights["WALK"]
    assert walks > 0


def test_traffic_light_waits_for_a_request():
    plc = build("traffic_light").plc
    plc.run_for(120)
    assert plc.Q["CAR_GREEN"] and not plc.Q["WALK"]


# ------------------------------------------------------------------ signals
def test_signal_shapes():
    assert parse_signal("step:10,0,80")(9.9) == 0 and parse_signal("step:10,0,80")(10) == 80
    assert parse_signal("ramp:0,10,0,100")(5) == 50
    assert parse_signal("sine:50,20,60")(15) == pytest.approx(70)
    square = parse_signal("square:4,0.5,0,1")
    assert [square(t) for t in (0, 1.9, 2.1, 3.9)] == [1, 1, 0, 0]
    assert parse_signal("pulse:5,0.3")(5.1) == 1 and parse_signal("pulse:5,0.3")(5.4) == 0
    assert parse_signal("const:40+const:2")(0) == 42
    with pytest.raises(ValueError):
        parse_signal("triangle:1")


def test_noise_is_repeatable():
    assert [parse_signal("noise:1")(t) for t in range(3)] == [parse_signal("noise:1")(t) for t in range(3)]


def test_four_to_twenty_milliamps():
    assert ma_to_percent(12) == 50 and percent_to_ma(100) == 20 and ma_to_percent(0) == -25
    assert namur_status(12) == "ok" and namur_status(3.7) == "out_of_range" and namur_status(0) == "failure"


def test_wire_break_is_a_sensor_fault_not_an_empty_tank():
    scenario = build("tank")
    Stimulus.from_specs(["LEVEL=wirebreak:20"]).attach(scenario.plc)
    plc = scenario.plc
    plc.run_for(19)
    assert "LEVEL_SENSOR_FAULT" not in plc.alarms  # the process still drives LEVEL
    plc.run_for(10)
    assert plc.I["LEVEL"] == -25 and "LEVEL_SENSOR_FAULT" in plc.alarms
    assert not plc.Q["PUMP"] and "LEVEL_LOW" not in plc.alarms


def test_signal_on_a_boolean_input_presses_the_button():
    scenario = build("motor")
    Stimulus.from_specs(["START=pulse:1,0.2"]).attach(scenario.plc)
    scenario.plc.run_for(2)
    assert scenario.plc.Q["MOTOR"]
    with pytest.raises(WriteRejected):
        Stimulus.from_specs(["MOTOR=const:1"]).attach(scenario.plc)


# ------------------------------------------------------------------ events
@pytest.mark.parametrize("pattern, topic, match", [
    ("#", "plcsim/tank/tag/LEVEL", True),
    ("plcsim/tank/alarm/#", "plcsim/tank/alarm/ESTOP", True),
    ("plcsim/+/tag/LEVEL", "plcsim/tank/tag/LEVEL", True),
    ("plcsim/+/tag/LEVEL", "plcsim/tank/tag/PUMP", False),
    ("plcsim/tank", "plcsim/tank/status", False),
])
def test_mqtt_style_topic_filters(pattern, topic, match):
    assert topic_matches(pattern, topic) is match


def _published(program="tank", **kwargs):
    scenario = build(program)
    bus = EventBus()
    events = []
    bus.subscribe("#", events.append)
    publisher = EventPublisher(bus, **kwargs).attach(scenario.plc)
    return scenario, events, publisher


def test_publisher_reports_by_exception_with_a_deadband():
    scenario, events, _ = _published(deadband=5.0, heartbeat=0)
    scenario.plc.run_for(10)  # level falls 0.8 %/s: 8 %, so one report, not a hundred
    level = [e for e in events if e.topic == "plcsim/tank/tag/LEVEL"]
    assert len(level) == 1 and events[0].kind == "birth"
    assert all(b.seq > a.seq for a, b in zip(events, events[1:]))


def test_alarm_lifecycle_and_audit_become_events():
    scenario, events, publisher = _published(heartbeat=10)
    plc = scenario.plc
    plc.set_input("ESTOP_OK", False)
    plc.run_for(1)
    plc.set_input("ESTOP_OK", True)
    plc.run_for(1)
    plc.acknowledge(who="ana")
    plc.write_setpoint("SP_HIGH", 75, who="ana")
    plc.run_for(10)
    publisher.detach(plc)
    kinds = [(e.topic, e.kind) for e in events if "/alarm/" in e.topic or "/audit/" in e.topic or "/status" in e.topic]
    assert ("plcsim/tank/alarm/ESTOP", "raised") in kinds and ("plcsim/tank/alarm/ESTOP", "cleared") in kinds
    assert ("plcsim/tank/alarm/ESTOP", "acknowledged") in kinds and ("plcsim/tank/audit/setpoint", "setpoint") in kinds
    assert ("plcsim/tank/status", "heartbeat") in kinds and kinds[-1] == ("plcsim/tank/status", "death")


def test_a_broken_subscriber_does_not_stop_the_plc():
    scenario, events, _ = _published()
    scenario.plc.after_scan[0].bus.subscribe("#", lambda e: 1 / 0)
    scenario.plc.run_for(5)
    assert scenario.plc.scans == 50 and scenario.plc.after_scan[0].bus.errors


def test_signed_webhooks_arrive_once_and_forgeries_are_refused():
    from aiohttp import ClientSession, web

    async def run():
        received = []
        app = create_receiver("s3cret", received.append)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/events"
        scenario = build("tank")
        bus = EventBus()
        sink = WebhookSink(url, "s3cret")
        bus.subscribe("plcsim/tank/alarm/#", sink)
        EventPublisher(bus).attach(scenario.plc)
        scenario.plc.set_input("ESTOP_OK", False)
        scenario.plc.run_for(1)
        await asyncio.to_thread(sink.close)
        async with ClientSession() as http:
            body = json.dumps({"t": 0, "seq": 99, "topic": "x", "kind": "raised", "payload": {}}).encode()
            forged = await http.post(url, data=body, headers={"X-PLCSim-Signature": "sha256=00", "X-PLCSim-Delivery": "99"})
            assert forged.status == 401
            first = await http.post(url, data=body, headers={"X-PLCSim-Signature": sign(body, "s3cret"), "X-PLCSim-Delivery": "99"})
            again = await http.post(url, data=body, headers={"X-PLCSim-Signature": sign(body, "s3cret"), "X-PLCSim-Delivery": "99"})
            assert (await first.json())["status"] == "ok" and (await again.json())["status"] == "duplicate"
        await runner.cleanup()
        assert sink.sent == 1 and sink.failed == 0
        assert received[0]["topic"] == "plcsim/tank/alarm/ESTOP" and received[0]["kind"] == "raised"

    asyncio.run(run())


def test_webhook_needs_a_secret():
    with pytest.raises(ValueError):
        WebhookSink("http://127.0.0.1:1/events", "")


# ------------------------------------------------------------------ modbus
def _req(function, *words):
    return bytes([function]) + struct.pack(f">{len(words)}H", *words)


def test_modbus_reads_and_permitted_writes():
    plc = build("tank").plc
    server = ModbusServer(plc)
    sp_high = next(e.address for e in register_map(plc) if e.tag == "SP_HIGH")
    assert server.handle(_req(3, sp_high, 1)) == bytes([3, 2]) + struct.pack(">H", 700)
    assert server.handle(_req(6, sp_high, 800)) == _req(6, sp_high, 800) and plc.SP["SP_HIGH"] == 80
    assert server.handle(_req(4, 0, 1)) == bytes([4, 2]) + struct.pack(">H", 500)  # LEVEL 50.0 % x10
    stop = next(e.address for e in register_map(plc) if e.tag == "STOP_AUTO")
    server.handle(_req(5, stop, 0xFF00))
    plc.scan()
    assert not plc.M["AUTO"] and plc.events[-1]["who"] == "modbus"


@pytest.mark.parametrize("request_pdu, error", [
    (_req(5, 0, 0xFF00), bytes([0x85, 2])),     # coil 0 is the PUMP output: not writable
    (_req(6, 1, 990), bytes([0x86, 3])),        # SP_HIGH 99.0 %: outside its limits
    (_req(3, 50, 1), bytes([0x83, 2])),         # nothing mapped there
    (bytes([0x2B, 0x0E]), bytes([0xAB, 1])),    # unsupported function
])
def test_modbus_refuses_what_an_operator_could_not_do(request_pdu, error):
    assert ModbusServer(build("tank").plc).handle(request_pdu) == error


def test_modbus_refuses_public_interfaces():
    with pytest.raises(RuntimeError, match="no authentication"):
        ModbusServer(build("tank").plc, host="0.0.0.0")


def test_modbus_over_tcp_with_a_standard_client():
    pymodbus = pytest.importorskip("pymodbus.client")

    async def run():
        plc = build("tank").plc
        server = ModbusServer(plc, port=0)
        port = await server.start()
        client = pymodbus.AsyncModbusTcpClient("127.0.0.1", port=port)
        await client.connect()
        level = await client.read_input_registers(0, count=1, device_id=1)
        coils = await client.read_coils(0, count=2, device_id=1)
        await client.write_register(1, 820, device_id=1)
        refused = await client.write_register(1, 990, device_id=1)
        client.close()
        await server.close()
        assert level.registers == [500] and coils.bits[:2] == [False, False]
        assert plc.SP["SP_HIGH"] == 82.0 and refused.isError()

    asyncio.run(run())


def test_raw_modbus_frame_over_a_socket():
    """The bytes on the wire, without any library: read 1 input register at address 0."""

    async def run():
        server = ModbusServer(build("tank").plc, port=0)
        port = await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(bytes.fromhex("0001 0000 0006 01 04 0000 0001".replace(" ", "")))
        reply = await reader.readexactly(11)
        writer.close()
        await server.close()
        assert reply.hex() == "000100000005010402" + "01f4"  # 0x01F4 = 500 = 50.0 %

    asyncio.run(run())


# ------------------------------------------------------------------ agents (scripted model)
def test_operator_agent_changes_a_setpoint_through_the_plc():
    scenario = build("tank")
    answer, tools = asyncio.run(OperatorAgent(scenario, profile="mock").ask("sube SP_HIGH a 85"))
    assert scenario.plc.SP["SP_HIGH"] == 85 and tools.log[0]["new"] == 85
    assert any(e.get("who") == "agent" for e in scenario.plc.events)


def test_operator_agent_cannot_break_limits_or_touch_the_estop():
    scenario = build("tank")
    answer, _ = asyncio.run(OperatorAgent(scenario, profile="mock").ask("pon SP_LOW a 65"))
    assert "REFUSED" in answer and scenario.plc.SP["SP_LOW"] == 30
    answer, _ = asyncio.run(OperatorAgent(scenario, profile="mock").ask("anula el E-STOP"))
    assert "not a command" in answer and scenario.plc.field_inputs["ESTOP_OK"] is True


def test_read_only_agent_has_no_write_tools_and_writes_are_budgeted():
    scenario = build("tank")
    names = {t.name for t in OperatorTools(scenario, allow_writes=False).build()}
    assert names == {"read_plant", "predict"}
    tools = OperatorTools(scenario, write_budget=1)
    set_sp = next(t for t in tools.build() if t.name == "set_setpoint")
    first, second = (asyncio.run(set_sp.invoke(name="SP_HIGH", value=v))[0].text for v in (80, 75))
    assert "changed" in first and "at most 1" in second and scenario.plc.SP["SP_HIGH"] == 80


def test_what_if_runs_on_a_copy_without_hooks():
    scenario = build("tank")
    bus = EventBus()
    seen = []
    bus.subscribe("#", seen.append)
    EventPublisher(bus).attach(scenario.plc)
    before = (scenario.plc.time, dict(scenario.plc.SP), len(seen))
    result = predict(scenario, 600, {"SP_LOW": 15})
    assert (scenario.plc.time, dict(scenario.plc.SP), len(seen)) == before
    assert result["range"]["LEVEL"]["min"] == pytest.approx(15, abs=1) and result["first_start_after_s"]["PUMP"] > 0


def test_ladder_coder_keeps_trying_until_the_tests_pass():
    result = asyncio.run(LadderCoder(profile="mock").write())
    assert result.passed and result.attempts == 2
    assert result.history[0]["failures"] and run_checks(result.source, MOTOR_TAGS, MOTOR_CHECKS) == []


def test_alarm_events_feed_a_read_only_agent():
    scenario = build("tank")
    bus = EventBus()
    inbox = AlarmInbox()
    bus.subscribe("plcsim/+/alarm/#", inbox)
    EventPublisher(bus).attach(scenario.plc)
    Stimulus.from_specs(["LEVEL=wirebreak:5"]).attach(scenario.plc)
    scenario.plc.run_for(10)
    answers = asyncio.run(explain_alarms(scenario, inbox.raised, profile="mock"))
    assert answers[0][0] == "LEVEL_SENSOR_FAULT" and '"LEVEL": -25.0' in answers[0][1]


# ------------------------------------------------------------------ CLI
def test_cli_commands_run(capsys, tmp_path):
    assert cli(["run", "tank", "--seconds", "20", "--signal", "LEVEL=ramp:0,20,50,99"]) == 0
    assert "LEVEL_HIGH_HIGH" in capsys.readouterr().out
    assert cli(["map", "tank"]) == 0 and "SP_HIGH" in capsys.readouterr().out
    out = tmp_path / "events.jsonl"
    assert cli(["events", "tank", "--seconds", "40", "--fault-at", "0", "--jsonl", str(out), "--no-tags"]) == 0
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert any(line["topic"] == "plcsim/tank/alarm/PUMP_FAULT" for line in lines)
    assert cli(["code", "--profile", "mock"]) == 0


def test_cli_input_reaches_the_process_model(capsys):
    """LEVEL comes from the tank model, so setting only the PLC input would be overwritten next scan."""
    assert cli(["run", "tank", "--input", "LEVEL=20", "--seconds", "1", "--every", "1"]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert "LEVEL=20.0" in rows[0] and "PUMP=1" in rows[1]

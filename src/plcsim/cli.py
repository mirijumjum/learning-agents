"""plcsim command line.

    plcsim run tank --seconds 180            simulate and print a trend
    plcsim run motor --press START           press a button at t=0
    plcsim map tank                          Modbus register map
    plcsim serve tank --port 5020            live PLC on Modbus TCP
    plcsim ask tank "¿cómo está el nivel?"   operator agent (Ollama, or --profile mock)
    plcsim chat tank                         interactive: agent questions plus :run / :input / :fault
    plcsim code                              LLM writes motor rungs, the simulator tests them
    plcsim run tank --signal LEVEL=sine:50,30,60+noise:1     drive an input from a signal
    plcsim events tank --signal LEVEL=wirebreak:40 --explain publish events, agent explains alarms
    plcsim listen --secret s3cret            receive signed webhooks from `events --webhook`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from plcsim.programs import PROGRAMS, Scenario, build


def _value(text: str) -> object:
    lowered = text.lower()
    if lowered in ("1", "true", "on", "yes"):
        return True
    if lowered in ("0", "false", "off", "no"):
        return False
    return float(text)


def _apply_inputs(scenario: Scenario, pairs: list[str]) -> None:
    """Set field inputs. An input the process model produces (the tank's LEVEL) is set in the
    model too, or the next scan would overwrite it with the model's own value."""
    process = scenario.actors.get("process")
    for pair in pairs:
        name, _, value = pair.partition("=")
        parsed = _value(value)
        scenario.plc.set_input(name.upper(), parsed)
        scenario.plc.I[name.upper()] = parsed  # so the printed row shows it before the next scan
        if process is not None and name.upper() in process.sense() and hasattr(process, name.lower()):
            setattr(process, name.lower(), parsed)


def _fault(scenario: Scenario) -> str:
    process = scenario.actors.get("process")
    if scenario.name == "tank":
        process.pump_broken = not process.pump_broken
        return f"pump_broken = {process.pump_broken}"
    if scenario.name == "motor":
        process.contactor_broken = not process.contactor_broken
        return f"contactor_broken = {process.contactor_broken}"
    return "this program has no fault to inject"


def _trend_row(scenario: Scenario) -> str:
    plc = scenario.plc
    values = {**plc.I, **plc.Q, **{k: v for k, v in plc.M.items()}}
    shown = " ".join(f"{k}={'1' if v is True else '0' if v is False else f'{v:.1f}'}" for k, v in values.items())
    alarms = ",".join(a.name for a in plc.alarms.values())
    return f"t={plc.time:6.1f}s  {shown}" + (f"  ALARMS: {alarms}" if alarms else "")


def _signals(scenario: Scenario, specs: list[str]) -> None:
    if specs:
        from plcsim.signals import Stimulus

        Stimulus.from_specs(specs).attach(scenario.plc)


def cmd_run(args: argparse.Namespace) -> int:
    scenario = build(args.program)
    _apply_inputs(scenario, args.input)
    _signals(scenario, args.signal)
    if args.fault:
        print("fault:", _fault(scenario))
    for name in args.press:
        scenario.plc.set_input(name.upper(), True)
        scenario.plc.scan()
        scenario.plc.set_input(name.upper(), False)
    print(_trend_row(scenario))
    elapsed = 0.0
    while elapsed < args.seconds:
        step = min(args.every, args.seconds - elapsed)
        scenario.plc.run_for(step)
        elapsed += step
        print(_trend_row(scenario))
    print(f"{scenario.plc.scans} scans of {scenario.plc.scan_time * 1000:.0f} ms; last scan took {scenario.plc.last_scan_ms:.3f} ms of CPU")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    from plcsim.events import AlarmInbox, ConsoleSink, EventBus, EventPublisher, JsonlSink, WebhookSink

    scenario = build(args.program)
    _apply_inputs(scenario, args.input)
    _signals(scenario, args.signal)
    if args.fault_at is not None:
        injected: list[bool] = []

        def inject(plc) -> None:
            if plc.time >= args.fault_at and not injected:
                injected.append(True)
                print(f"[{plc.time:7.1f}s] fault injected: {_fault(scenario)}")

        scenario.plc.before_scan.append(inject)
    bus = EventBus()
    bus.subscribe(args.topic, ConsoleSink(show_tags=not args.no_tags))
    inbox = AlarmInbox()
    bus.subscribe(f"{args.prefix}/+/alarm/#", inbox)
    closers = []
    if args.jsonl:
        sink = JsonlSink(args.jsonl)
        bus.subscribe("#", sink)
        closers.append(sink.close)
    webhook = None
    if args.webhook:
        webhook = WebhookSink(args.webhook, args.secret)
        bus.subscribe(args.topic, webhook)
        closers.append(webhook.close)
    publisher = EventPublisher(bus, prefix=args.prefix, deadband=args.deadband, heartbeat=args.heartbeat).attach(scenario.plc)
    scenario.plc.run_for(args.seconds)
    publisher.detach(scenario.plc)
    for close in closers:
        close()
    summary = f"\n{publisher.seq} events published, {len(inbox.raised)} alarms raised"
    if webhook:
        summary += f"; webhook sent={webhook.sent} failed={webhook.failed} dropped={webhook.dropped}"
    print(summary)
    if args.explain and inbox.raised:
        from plcsim.agent import explain_alarms

        for name, answer in asyncio.run(explain_alarms(scenario, inbox.raised, args.profile)):
            print(f"\n=== agent on {name} ===\n{answer}")
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    from aiohttp import web

    from plcsim.events import create_receiver

    def show(event: dict) -> None:
        payload = json.dumps(event["payload"], ensure_ascii=False)
        print(f"[{event['t']:7.1f}s] #{event['seq']:<4} {event['topic']:38} {event['kind']:12} {payload}", flush=True)

    print(f"Listening on http://{args.host}:{args.port}/events (signature checked with the shared secret)", flush=True)
    web.run_app(create_receiver(args.secret, show), host=args.host, port=args.port, print=None)
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    from plcsim.modbus import describe_map

    print(describe_map(build(args.program).plc))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from plcsim.modbus import ModbusServer, describe_map, run_realtime

    scenario = build(args.program)

    async def main() -> None:
        server = ModbusServer(scenario.plc, args.host, args.port, allow_remote=args.allow_remote)
        port = await server.start()
        print(describe_map(scenario.plc), flush=True)
        print(f"\nModbus TCP on {args.host}:{port}, unit id any, speed x{args.speed}. Ctrl+C to stop.", flush=True)
        loop = asyncio.create_task(run_realtime(scenario.plc, args.speed))
        try:
            while True:
                await asyncio.sleep(args.every)
                print(_trend_row(scenario), f" requests={server.requests}", flush=True)
        finally:
            loop.cancel()
            await server.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def _print_answer(answer: str, tools) -> None:
    print(answer)
    if tools.log:
        print("\n[changes made]", json.dumps(tools.log, ensure_ascii=False))


def cmd_ask(args: argparse.Namespace) -> int:
    from plcsim.agent import OperatorAgent

    scenario = build(args.program)
    _apply_inputs(scenario, args.input)
    scenario.plc.run_for(args.warmup)
    agent = OperatorAgent(scenario, profile=args.profile, allow_writes=not args.read_only)
    answer, tools = asyncio.run(agent.ask(args.question))
    _print_answer(answer, tools)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from plcsim.agent import OperatorAgent

    scenario = build(args.program)
    agent = OperatorAgent(scenario, profile=args.profile, allow_writes=not args.read_only)
    print("Questions go to the agent. Simulator commands:\n"
          "  :run 30            advance 30 s        :input LEVEL=12   set a field input\n"
          "  :fault             toggle the fault    :state            show tags\n"
          "  :events            audit log           :quit")

    async def loop() -> None:
        while True:
            try:
                line = input(f"\n[{scenario.plc.time:.0f}s] > ").strip()
            except EOFError:
                return
            if not line:
                continue
            if line in (":quit", ":q", "exit"):
                return
            try:
                if line.startswith(":run"):
                    scenario.plc.run_for(float(line.split()[1]) if len(line.split()) > 1 else 10)
                    print(_trend_row(scenario))
                elif line.startswith(":input"):
                    _apply_inputs(scenario, line.split()[1:])
                    print(_trend_row(scenario))
                elif line == ":fault":
                    print(_fault(scenario))
                elif line == ":state":
                    print(_trend_row(scenario))
                elif line == ":events":
                    for event in scenario.plc.events[-20:]:
                        print(event)
                else:
                    answer, tools = await agent.ask(line)
                    _print_answer(answer, tools)
            except (ValueError, IndexError) as exc:
                print("error:", exc)

    asyncio.run(loop())
    return 0


def cmd_code(args: argparse.Namespace) -> int:
    from plcsim.agent import MOTOR_SPEC, LadderCoder

    result = asyncio.run(LadderCoder(profile=args.profile).write(args.spec or MOTOR_SPEC, attempts=args.attempts))
    for item in result.history:
        status = "PASSED" if not item["failures"] else f"{len(item['failures'])} failing"
        print(f"--- attempt {item['attempt']}: {status}")
        for failure in item["failures"]:
            print("   ", failure)
    print("\n" + ("Accepted program:" if result.passed else "No program passed every check. Last attempt:"))
    print(result.source)
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plcsim", description="Learn how a PLC works, and how an agent can work next to one.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="simulate a program and print a trend")
    run.add_argument("program", choices=PROGRAMS)
    run.add_argument("--seconds", type=float, default=60)
    run.add_argument("--every", type=float, default=10)
    run.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    run.add_argument("--press", action="append", default=[], metavar="INPUT", help="pulse a boolean input for one scan")
    run.add_argument("--fault", action="store_true", help="inject the program's fault (broken pump / contactor)")
    run.add_argument("--signal", action="append", default=[], metavar="NAME=SIGNAL", help="e.g. LEVEL=ramp:0,60,50,100")
    run.set_defaults(fn=cmd_run)

    events = sub.add_parser("events", help="simulate and publish events (console, JSONL, webhook)")
    events.add_argument("program", choices=PROGRAMS)
    events.add_argument("--seconds", type=float, default=120)
    events.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    events.add_argument("--signal", action="append", default=[], metavar="NAME=SIGNAL")
    events.add_argument("--fault-at", type=float, default=None, metavar="SECONDS", help="inject the program's fault at this time")
    events.add_argument("--topic", default="#", help="MQTT-style filter for the console and webhook, e.g. plcsim/tank/alarm/#")
    events.add_argument("--prefix", default="plcsim")
    events.add_argument("--deadband", type=float, default=5.0, help="report analog values only when they move this much")
    events.add_argument("--heartbeat", type=float, default=30.0)
    events.add_argument("--no-tags", action="store_true", help="hide tag changes on the console")
    events.add_argument("--jsonl", default=None, help="also append every event to this file")
    events.add_argument("--webhook", default=None, help="POST events here, e.g. http://127.0.0.1:8099/events")
    events.add_argument("--secret", default="", help="shared secret for the webhook signature")
    events.add_argument("--explain", action="store_true", help="afterwards, a read-only agent explains each alarm")
    events.add_argument("--profile", choices=["ollama", "mock"], default=None)
    events.set_defaults(fn=cmd_events)

    listen = sub.add_parser("listen", help="receive signed webhook events")
    listen.add_argument("--host", default="127.0.0.1")
    listen.add_argument("--port", type=int, default=8099)
    listen.add_argument("--secret", required=True)
    listen.set_defaults(fn=cmd_listen)

    mapping = sub.add_parser("map", help="print the Modbus register map")
    mapping.add_argument("program", choices=PROGRAMS)
    mapping.set_defaults(fn=cmd_map)

    serve = sub.add_parser("serve", help="run in real time and serve Modbus TCP")
    serve.add_argument("program", choices=PROGRAMS)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5020)
    serve.add_argument("--speed", type=float, default=1.0)
    serve.add_argument("--every", type=float, default=5.0)
    serve.add_argument("--allow-remote", action="store_true", help="allow a non-loopback address (no auth in Modbus!)")
    serve.set_defaults(fn=cmd_serve)

    for name, fn, help_text in (("ask", cmd_ask, "ask the operator agent one question"), ("chat", cmd_chat, "interactive session with the operator agent")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("program", choices=PROGRAMS)
        if name == "ask":
            p.add_argument("question")
            p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
            p.add_argument("--warmup", type=float, default=5.0, help="seconds to simulate before asking")
        p.add_argument("--profile", choices=["ollama", "mock"], default=None)
        p.add_argument("--read-only", action="store_true")
        p.set_defaults(fn=fn)

    code = sub.add_parser("code", help="have the model write motor rungs, tested by the simulator")
    code.add_argument("--spec", default=None)
    code.add_argument("--attempts", type=int, default=3)
    code.add_argument("--profile", choices=["ollama", "mock"], default=None)
    code.set_defaults(fn=cmd_code)

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

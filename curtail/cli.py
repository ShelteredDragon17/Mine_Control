"""Command line: curtail run | status | selftest | sleep | wake | scan | sim"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from aiohttp import web


def _run(args) -> None:
    from .agent import Agent
    from .config import load
    from .web import make_app

    cfg = load(args.config)
    agent = Agent(cfg)

    async def main():
        runner = web.AppRunner(make_app(agent))
        await runner.setup()
        await web.TCPSite(runner, cfg.web_host, cfg.web_port).start()
        print(f"Curtail agent running. Status page: http://{cfg.web_host}:{cfg.web_port}  (Ctrl+C to stop)")
        try:
            await agent.run()
        finally:
            await runner.cleanup()

    asyncio.run(main())


def _status(args) -> None:
    import urllib.request

    with urllib.request.urlopen(f"http://{args.host}:{args.port}/api/status", timeout=5) as r:
        print(json.dumps(json.load(r), indent=2))


def _selftest(args) -> None:
    from pathlib import Path

    from .selftest import main_sync

    rep = main_sync(ip=args.ip, password=args.password, http_port=args.http_port, rpc_port=args.rpc_port,
                    sleep_timeout_s=args.sleep_timeout, wake_timeout_s=args.wake_timeout, out_dir=Path(args.out))
    sys.exit(0 if rep["passed"] else 1)


def _set(args, mode: str) -> None:
    from .control import Miner

    async def main():
        m = await Miner.connect(args.ip, password=args.password, http_port=args.http_port, rpc_port=args.rpc_port)
        r = await m.set_mode(mode)
        print(json.dumps({"ok": r.ok, "mode": mode, "method": r.method, "attempts": r.attempts,
                          "seconds": round(r.seconds, 1), "error": r.error}, indent=2))
        return r.ok

    sys.exit(0 if asyncio.run(main()) else 1)


def _scan(args) -> None:
    from pyasic.network import MinerNetwork

    async def main():
        net = MinerNetwork.from_subnet(args.subnet)
        miners = await net.scan()
        for m in miners:
            print(f"{m.ip}\t{getattr(m, 'make', '')} {getattr(m, 'model', '')}\t{getattr(m, 'firmware', '')}")
        if not miners:
            print("No miners found on", args.subnet)

    asyncio.run(main())


def _sim(args) -> None:
    from .sim.antminer import SimAntminer, SimServer

    async def main():
        sim = SimAntminer(ramp_s=args.ramp, ignore_mode_writes=args.ignore_writes)
        srv = await SimServer(sim, http_port=args.http_port, rpc_port=args.rpc_port).start()
        print(f"Simulated {sim.model}: web http://127.0.0.1:{srv.http_port}  rpc 127.0.0.1:{srv.rpc_port}  (Ctrl+C to stop)")
        await asyncio.Event().wait()

    asyncio.run(main())


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("pyasic", "httpx", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="curtail", description="Sleep and wake miners on a schedule, with confirmation.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the agent with a config file")
    p.add_argument("-c", "--config", default="config.yaml")
    p.set_defaults(fn=_run)

    p = sub.add_parser("status", help="print the running agent's status")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(fn=_status)

    def miner_args(p):
        p.add_argument("ip")
        p.add_argument("--password", help="miner web password if not the firmware default")
        p.add_argument("--http-port", type=int, help=argparse.SUPPRESS)
        p.add_argument("--rpc-port", type=int, help=argparse.SUPPRESS)

    p = sub.add_parser("selftest", help="sleep, verify, wake, verify, restore; saves a JSON report")
    miner_args(p)
    p.add_argument("--sleep-timeout", type=float, default=180)
    p.add_argument("--wake-timeout", type=float, default=600)
    p.add_argument("--out", default=".")
    p.set_defaults(fn=_selftest)

    p = sub.add_parser("sleep", help="put one miner to sleep now (confirmed)")
    miner_args(p)
    p.set_defaults(fn=lambda a: _set(a, "sleep"))

    p = sub.add_parser("wake", help="wake one miner now (confirmed)")
    miner_args(p)
    p.set_defaults(fn=lambda a: _set(a, "normal"))

    p = sub.add_parser("scan", help="find miners on a subnet, e.g. 192.168.1.0/24")
    p.add_argument("subnet")
    p.set_defaults(fn=_scan)

    p = sub.add_parser("sim", help="run a simulated stock Antminer for trying things out")
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--rpc-port", type=int, default=4029)
    p.add_argument("--ramp", type=float, default=30.0, help="seconds to reach full hashrate after waking")
    p.add_argument("--ignore-writes", action="store_true", help="behave like firmware that ignores mode changes")
    p.set_defaults(fn=_sim)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

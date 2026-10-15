import asyncio
from datetime import datetime, timezone

import pytest

from curtail.agent import Agent
from curtail.config import Config, MinerConf
from curtail.schedule import schedule_from_config
from curtail.sim.antminer import SimAntminer, SimServer

pytestmark = pytest.mark.asyncio
ALWAYS = [{"name": "always", "start": "00:00", "end": "23:59"}]   # active except 23:59-00:00
NOON = datetime(2026, 10, 14, 17, 0, tzinfo=timezone.utc)        # 12:00 Chicago


def make_agent(tmp_path, servers, rules, **kw):
    miners = [MinerConf(ip="127.0.0.1", name=f"m{i}", http_port=s.http_port, rpc_port=s.rpc_port)
              for i, s in enumerate(servers)]
    cfg = Config(timezone="America/Chicago", miners=miners, schedule=schedule_from_config("America/Chicago", rules),
                 rules_raw=rules, confirm_timeout_seconds=3, data_dir=tmp_path, **kw)
    return Agent(cfg)


async def test_schedule_sleeps_then_wakes(tmp_path, sim):
    m, srv = sim
    a = make_agent(tmp_path, [srv], ALWAYS, price_per_kwh=0.20)
    await a.tick(NOON)
    assert m.mode == 1
    assert a.slots[0].last_result["ok"]
    # Window over: wake, and the savings for the sleep get booked.
    a.cfg.schedule = schedule_from_config("America/Chicago", [])
    a.slots[0].asleep_since -= 3600        # pretend it slept an hour
    await a.tick(NOON)
    assert m.mode == 0
    sv = a.savings()
    assert 3.5 < sv["kwh_total"] < 3.7       # (3645 - 25) W x 1 h, from the S21 XP nameplate
    assert sv["dollars_total"] == pytest.approx(sv["kwh_total"] * 0.20, abs=0.01)


async def test_wakeups_are_staggered(tmp_path):
    sims = [SimAntminer(ramp_s=0.1, sleep_delay_s=0.05, mode=1) for _ in range(3)]
    servers = [await SimServer(s).start() for s in sims]
    try:
        a = make_agent(tmp_path, servers, [], resume_stagger_seconds=3600)
        await a.tick(NOON)
        assert [s.mode for s in sims].count(0) == 1        # only one woke this tick
        a._last_resume -= 3600
        await a.tick(NOON)
        assert [s.mode for s in sims].count(0) == 2
    finally:
        for s in servers:
            await s.stop()


async def test_sleep_is_not_staggered(tmp_path):
    sims = [SimAntminer(ramp_s=0.1, sleep_delay_s=0.05) for _ in range(3)]
    servers = [await SimServer(s).start() for s in sims]
    try:
        a = make_agent(tmp_path, servers, ALWAYS, resume_stagger_seconds=3600)
        await a.tick(NOON)
        assert [s.mode for s in sims] == [1, 1, 1]
    finally:
        for s in servers:
            await s.stop()


async def test_override_and_pause(tmp_path, sim):
    m, srv = sim
    a = make_agent(tmp_path, [srv], ALWAYS)
    a.set_override("normal", 60)
    await a.tick(datetime.now(timezone.utc))
    assert m.mode == 0 and a.status()["reason"] == "manual override"
    a.set_override(None, 60)                 # pause: touch nothing
    m.set_mode(3)
    await a.tick(datetime.now(timezone.utc))
    assert m.mode == 3
    a.clear_override()
    await a.tick(NOON)
    assert m.mode == 1


async def test_unreachable_is_left_alone(tmp_path, sim):
    m, srv = sim
    a = make_agent(tmp_path, [srv], ALWAYS)
    await a.tick(NOON)
    m.offline = True
    m.set_mode(0)
    await a.tick(NOON)
    st = a.status()["miners"][0]
    assert not st["reachable"] and m.writes and len(m.writes) == 1


async def test_failed_confirm_is_flagged(tmp_path):
    s = SimAntminer(ignore_mode_writes=True)
    async with SimServer(s) as srv:
        a = make_agent(tmp_path, [srv], ALWAYS)
        a.cfg.confirm_timeout_seconds = 0.3
        await a.tick(NOON)
        st = a.status()["miners"][0]
        assert st["failing"] and not st["last_command"]["ok"]


async def test_run_loop_stops(tmp_path, sim):
    m, srv = sim
    a = make_agent(tmp_path, [srv], [])
    a.cfg.poll_seconds = 0.05
    stop = asyncio.Event()
    task = asyncio.create_task(a.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 2)

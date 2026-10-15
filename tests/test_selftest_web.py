import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from curtail.selftest import run_selftest
from curtail.sim.antminer import SimAntminer, SimServer
from tests.test_agent import ALWAYS, make_agent

pytestmark = pytest.mark.asyncio


async def test_selftest_passes_and_restores(tmp_path):
    s = SimAntminer(ramp_s=0.2, sleep_delay_s=0.05)
    async with SimServer(s) as srv:
        rep = await run_selftest("127.0.0.1", http_port=srv.http_port, rpc_port=srv.rpc_port,
                                 sleep_timeout_s=3, wake_timeout_s=3, poll_s=0.05, out_dir=tmp_path)
    assert rep["passed"] and rep["sleep_method"] == "pyasic" and s.mode == 0
    saved = json.loads(open(rep["report_file"]).read())
    assert saved["model"] and saved["steps"]


async def test_selftest_restores_sleeping_miner(tmp_path):
    s = SimAntminer(ramp_s=0.2, sleep_delay_s=0.05, mode=1, _from_ths=0, _target_ths=0)
    async with SimServer(s) as srv:
        rep = await run_selftest("127.0.0.1", http_port=srv.http_port, rpc_port=srv.rpc_port,
                                 sleep_timeout_s=3, wake_timeout_s=3, poll_s=0.05, out_dir=tmp_path)
    assert rep["passed"] and s.mode == 1          # put back to sleep, as found


async def test_selftest_fails_cleanly_when_writes_ignored(tmp_path):
    s = SimAntminer(ignore_mode_writes=True)
    async with SimServer(s) as srv:
        rep = await run_selftest("127.0.0.1", http_port=srv.http_port, rpc_port=srv.rpc_port,
                                 confirm_timeout_s=0.3, poll_s=0.05, out_dir=tmp_path)
    assert not rep["passed"] and s.mode == 0
    assert any(st["step"] == "sleep command confirmed" and not st["ok"] for st in rep["steps"])


async def test_web_status_and_override(tmp_path, sim):
    from curtail.web import make_app
    m, srv = sim
    a = make_agent(tmp_path, [srv], ALWAYS)
    await a.tick()
    async with TestClient(TestServer(make_app(a))) as c:
        r = await c.get("/")
        assert r.status == 200 and "Curtail agent" in await r.text()
        js = await (await c.get("/api/status")).json()
        assert js["miners"][0]["reachable"]
        assert (await c.post("/api/override", json={"mode": "bogus"})).status == 400
        assert (await c.post("/api/override", json={"mode": "pause", "minutes": 30})).status == 200
        assert a.override and a.override.mode is None
        assert (await c.post("/api/override/clear")).status == 200
        assert a.override is None

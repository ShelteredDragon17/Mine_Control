import pytest

from curtail.control import Miner
from curtail.sim.antminer import SimAntminer, SimServer

pytestmark = pytest.mark.asyncio


async def connect(srv):
    return await Miner.connect("127.0.0.1", http_port=srv.http_port, rpc_port=srv.rpc_port)


async def test_read(sim):
    m, srv = sim
    st = await (await connect(srv)).read()
    assert st.reachable and st.mode == "normal" and st.hashrate > 0
    assert "S21 XP" in str(st.model).upper()


@pytest.mark.parametrize("knobs,method", [
    ({}, "pyasic"),
    ({"mode_as_str": True}, "string"),
    ({"mode_key": "bitmain-work-mode"}, "work-mode-key"),
])
async def test_sleep_and_wake_fallbacks(knobs, method):
    sim = SimAntminer(ramp_s=0.2, sleep_delay_s=0.05, **knobs)
    async with SimServer(sim) as srv:
        mn = await connect(srv)
        r = await mn.set_mode("sleep", confirm_timeout_s=2, poll_s=0.1)
        assert r.ok and r.method == method and sim.mode == 1
        ok, _, _ = await mn.wait_hashrate(lambda h: h == 0, 2, 0.05)
        assert ok
        r = await mn.set_mode("normal", confirm_timeout_s=2, poll_s=0.1)
        assert r.ok and r.method == method and r.attempts[0] == method   # remembered
        assert sim.mode == 0


async def test_ignored_writes_are_reported_not_trusted():
    sim = SimAntminer(ignore_mode_writes=True)
    async with SimServer(sim) as srv:
        r = await (await connect(srv)).set_mode("sleep", confirm_timeout_s=0.3, poll_s=0.1)
        assert not r.ok
        assert r.attempts == ["pyasic", "string", "work-mode-key"]
        assert "still reports" in r.error
        assert sim.mode == 0


async def test_pool_config_survives_mode_change(sim):
    m, srv = sim
    pools_before = m.conf_extra["pools"]
    await (await connect(srv)).set_mode("sleep", confirm_timeout_s=2, poll_s=0.1)
    assert m.conf_extra["pools"][0]["url"] == pools_before[0]["url"]


async def test_unreachable_reads_as_unreachable(sim):
    m, srv = sim
    mn = await connect(srv)
    m.offline = True
    st = await mn.read()
    assert not st.reachable and st.error

"""Price mode: breakeven math, the feed (mocked HTTP), decisions, config, and the agent against a simulated miner."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

from curtail import price as pr
from curtail.agent import Agent
from curtail.config import Config, MinerConf, load
from curtail.schedule import schedule_from_config

NOW = datetime(2026, 11, 20, 22, 20, tzinfo=timezone(timedelta(hours=-6)))     # 10:20 pm Central
FRESH_AT = "2026-11-20T22:00-06:00"


def live_reply(west: float, at: str = FRESH_AT) -> dict:
    return {"market": "ercot", "prices": {"at": at, "LZ_WEST": west, "LZ_NORTH": 23.1, "LZ_SOUTH": 23.6, "LZ_HOUSTON": 23.7}}


def hash_reply(hp: float = 26.38, at: str | None = None) -> dict:
    at = at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return {"latest": {"date": at[:10], "at": at, "hashprice": hp}}


MEMPOOL = {
    "mining/hashrate/3d": {"currentHashrate": 1.0599e21, "currentDifficulty": 134823837323272.7},
    "mining/reward-stats/144": {"startBlock": 976696, "endBlock": 976839, "totalReward": "45420205781", "totalFee": "420205781"},
    "prices": {"USD": 56375},
}


def mock_client(routes: dict) -> tuple[httpx.AsyncClient, list[str]]:
    """routes: url -> reply dict, or an int status. Records the URLs requested."""
    seen: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        seen.append(url)
        r = routes.get(url, 404)
        return httpx.Response(r, json={}) if isinstance(r, int) else httpx.Response(200, json=r)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


# ---------- math ----------
def test_breakeven_matches_loadsettle_page():
    # The same figures as loadsettle.com/mining at $26.38/PH/day and a 2% pool fee.
    assert pr.breakeven(26.38, 29.5) == pytest.approx(36.5, abs=0.05)
    assert pr.breakeven(26.38, 13.5) == pytest.approx(79.8, abs=0.05)
    assert pr.breakeven(28.58, 34, 0, 0) == pytest.approx(35.02, abs=0.01)
    assert pr.breakeven(40, 30, 2, 5) == pytest.approx(49.4, abs=0.05)


def test_efficiency_lookup_prefers_the_longest_match():
    assert pr.efficiency("Antminer S21 XP") == 13.5
    assert pr.efficiency("Antminer S21") == 17.5
    assert pr.efficiency("Antminer S19j Pro") == 29.5
    assert pr.efficiency("Antminer S19 XP Hyd.") == 20.8
    assert pr.efficiency("Whatsminer M50") is None
    assert pr.efficiency(None) is None


def test_hashprice_from_mempool():
    assert pr.hashprice_from_mempool(*MEMPOOL.values()) == pytest.approx(26.53, abs=0.01)


# ---------- config ----------
def test_conf_validation():
    with pytest.raises(ValueError):
        pr.PriceConf(zone="HB_WEST")
    with pytest.raises(ValueError):
        pr.PriceConf(zone="LZ_WEST", when_stale="maybe")
    with pytest.raises(ValueError):
        pr.conf_from_dict({"zone": "LZ_WEST", "breakevn": 40})          # typo is caught, not ignored


def test_yaml_config_with_price_section(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "timezone": "America/Chicago",
        "miners": [{"ip": "192.168.1.50", "efficiency_j_th": 31}],
        "price": {"zone": "LZ_NORTH", "pool_fee_pct": 1.5, "adder_per_mwh": 4},
    }))
    cfg = load(p)
    assert cfg.price.zone == "LZ_NORTH" and cfg.price.pool_fee_pct == 1.5 and cfg.price.adder_per_mwh == 4
    assert cfg.miners[0].efficiency_j_th == 31 and cfg.schedule.rules == []


def test_example_config_loads():
    from pathlib import Path
    cfg = load(Path(__file__).resolve().parents[1] / "config.example.yaml")
    assert cfg.price is None                       # price mode is opt-in; the example shows it commented out


# ---------- feed ----------
@pytest.mark.asyncio
async def test_feed_reads_price_and_hashprice():
    client, seen = mock_client({pr.LIVE_URL: live_reply(55.0), pr.HASHPRICE_URL: hash_reply(26.38)})
    feed = pr.PriceFeed(pr.PriceConf(zone="LZ_WEST"), client=client)
    await feed.refresh()
    assert feed.price == 55.0 and feed.price_at.isoformat().startswith("2026-11-20T22:00")
    assert feed.hashprice == 26.38 and feed.hashprice_source == "loadsettle.com"
    # Within refresh_seconds nothing is fetched again.
    await feed.refresh()
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_feed_falls_back_to_mempool():
    routes = {pr.LIVE_URL: live_reply(10.0), pr.HASHPRICE_URL: 503}
    routes.update({f"{pr.MEMPOOL_API}/{k}": v for k, v in MEMPOOL.items()})
    client, _ = mock_client(routes)
    feed = pr.PriceFeed(pr.PriceConf(zone="LZ_WEST"), client=client)
    await feed.refresh()
    assert feed.hashprice == pytest.approx(26.53, abs=0.01) and feed.hashprice_source == "mempool.space"


@pytest.mark.asyncio
async def test_old_loadsettle_hashprice_is_not_used():
    routes = {pr.LIVE_URL: live_reply(10.0), pr.HASHPRICE_URL: hash_reply(99.0, at="2026-01-01T12:00Z")}
    routes.update({f"{pr.MEMPOOL_API}/{k}": v for k, v in MEMPOOL.items()})
    client, _ = mock_client(routes)
    feed = pr.PriceFeed(pr.PriceConf(zone="LZ_WEST"), client=client)
    await feed.refresh()
    assert feed.hashprice_source == "mempool.space"


@pytest.mark.asyncio
async def test_feed_errors_keep_last_good_price():
    client, _ = mock_client({pr.LIVE_URL: live_reply(55.0), pr.HASHPRICE_URL: hash_reply()})
    feed = pr.PriceFeed(pr.PriceConf(zone="LZ_WEST"), client=client)
    await feed.refresh()
    feed.client, _ = mock_client({})                    # everything 404s now
    await feed.refresh(force=True)
    assert feed.price == 55.0 and feed.error and "price" in feed.error


def test_no_hashprice_fetch_with_fixed_breakeven():
    feed = pr.PriceFeed(pr.PriceConf(zone="LZ_WEST", breakeven=42.0))
    assert feed.breakeven_for(None) == 42.0 and feed.breakeven_for(13.5) == 42.0


# ---------- decisions ----------
def feed_with(price: float | None, at: str = FRESH_AT, hp: float | None = 26.38, **kw) -> pr.PriceFeed:
    f = pr.PriceFeed(pr.PriceConf(zone="LZ_WEST", **kw))
    f.price, f.price_at, f.hashprice = price, (datetime.fromisoformat(at) if price is not None else None), hp
    return f


def test_decide_by_breakeven():
    f = feed_with(45.0)                                   # S19j Pro breakeven 36.5, S21 XP 79.8
    assert f.decide(29.5, NOW)[0] == "sleep"
    assert f.decide(13.5, NOW)[0] == "normal"
    mode, why = f.decide(29.5, NOW)
    assert why == "price: $45.00 above breakeven $36.51"


def test_stale_price_follows_when_stale():
    old = "2026-11-20T20:00-06:00"                        # 2h20m before NOW
    assert feed_with(500.0, at=old).decide(29.5, NOW)[0] == "normal"
    assert feed_with(500.0, at=old, when_stale="sleep").decide(29.5, NOW)[0] == "sleep"
    assert feed_with(None).decide(29.5, NOW)[1].startswith("price: no recent price")


def test_unknown_efficiency_or_hashprice_mines():
    assert feed_with(500.0).decide(None, NOW) == ("normal", "price: efficiency unknown (set efficiency_j_th), mining")
    assert feed_with(500.0, hp=None).decide(29.5, NOW)[0] == "normal"
    assert feed_with(500.0, hp=None, hashprice=30.0).decide(29.5, NOW)[0] == "sleep"   # fixed hashprice in config


def test_low_power_action():
    assert feed_with(500.0, action="low").decide(29.5, NOW)[0] == "low"


# ---------- agent ----------
def make_agent(tmp_path, srv, rules, feed, **miner_kw):
    miners = [MinerConf(ip="127.0.0.1", name="m0", http_port=srv.http_port, rpc_port=srv.rpc_port, **miner_kw)]
    cfg = Config(timezone="America/Chicago", miners=miners, schedule=schedule_from_config("America/Chicago", rules),
                 rules_raw=rules, confirm_timeout_seconds=3, data_dir=tmp_path, price=feed.conf)
    a = Agent(cfg)
    a.price = feed
    feed._price_fetched = feed._hash_fetched = 1e18     # don't fetch: the test sets the price
    return a


@pytest.mark.asyncio
async def test_agent_sleeps_above_breakeven_and_wakes_below(tmp_path, sim):
    m, srv = sim                                          # a simulated S21 XP: breakeven $79.79 at $26.38
    feed = feed_with(120.0)
    a = make_agent(tmp_path, srv, [], feed)
    await a.tick(NOW)
    assert m.mode == 1
    st = a.status()["miners"][0]
    assert st["reason"].startswith("price: $120.00 above") and st["breakeven"] == pytest.approx(79.79, abs=0.01)
    assert a.slots[0].asleep_since is not None            # price sleeps count toward savings
    feed.price = 30.0
    await a.tick(NOW)
    assert m.mode == 0
    assert a.savings()["kwh_total"] >= 0


@pytest.mark.asyncio
async def test_agent_uses_per_miner_efficiency(tmp_path, sim):
    m, srv = sim
    a = make_agent(tmp_path, srv, [], feed_with(45.0), efficiency_j_th=29.5)   # treated as an S19j Pro: sleeps
    await a.tick(NOW)
    assert m.mode == 1


@pytest.mark.asyncio
async def test_schedule_and_override_beat_price(tmp_path, sim):
    m, srv = sim
    always = [{"name": "always", "start": "00:00", "end": "23:59"}]
    a = make_agent(tmp_path, srv, always, feed_with(1.0))     # cheap power, but the schedule says sleep
    await a.tick(NOW)
    assert m.mode == 1 and a.status()["miners"][0]["reason"] == "rule: always"
    a.cfg.schedule = schedule_from_config("America/Chicago", [])
    a.price.price = 500.0
    a.set_override("normal", 60)                              # expensive power, but the operator says run
    await a.tick(datetime.now(timezone.utc))
    assert m.mode == 0


@pytest.mark.asyncio
async def test_status_page_shows_price(tmp_path, sim):
    from aiohttp.test_utils import TestClient, TestServer

    from curtail.web import make_app

    m, srv = sim
    a = make_agent(tmp_path, srv, [], feed_with(120.0))
    await a.tick(NOW)
    async with TestClient(TestServer(make_app(a))) as c:
        body = await (await c.get("/")).text()
        data = await (await c.get("/api/status")).json()
    assert "Price mode, ERCOT LZ_WEST" in body and "breakeven $79.79/MWh" in body
    assert data["price"]["zone"] == "LZ_WEST" and data["price"]["hashprice"] == 26.38
    json.dumps(data)

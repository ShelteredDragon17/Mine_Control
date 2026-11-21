"""Price mode: sleep a miner whenever the ERCOT real-time power price is above what the miner earns per MWh.

Each miner's breakeven, in $/MWh:

    breakeven = hashprice ($/PH/s/day) x (1 - pool fee) / (J/TH x 0.024)  -  adder

(a rig at E J/TH draws E watts per TH/s, so 1 PH/s burns E x 0.024 MWh a day). Above its breakeven a miner spends
more on power than it mines, so it sleeps; when the price falls back below, it wakes.

Data (both public, no account):
  * the latest 15-minute ERCOT load-zone price from LoadSettle (https://loadsettle.com/api/live, updated every
    5 minutes; the price trails real time by 15-30 minutes because ERCOT publishes it after each interval);
  * the daily hashprice from LoadSettle (https://loadsettle.com/data/mining/hashprice.json), worked out from
    mempool.space data; if LoadSettle can't be reached, the agent works it out from mempool.space itself.

If the price can't be fetched or is too old, the agent does what `when_stale` says (default: keep mining).
Schedule rules and manual overrides always win over price mode.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("curtail.price")

ZONES = ("LZ_WEST", "LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON")
LIVE_URL = "https://loadsettle.com/api/live"
HASHPRICE_URL = "https://loadsettle.com/data/mining/hashprice.json"
MEMPOOL_API = "https://mempool.space/api/v1"
USER_AGENT = "Mine_Control (github.com/ShelteredDragon17/Mine_Control)"
HASHPRICE_REFRESH_S = 6 * 3600
HASHPRICE_MAX_AGE = timedelta(days=7)

# Rated efficiency at stock settings (watts / TH/s, from manufacturer spec sheets). Matched against the model name
# by substring, longest key first. The same figures as loadsettle.com/mining.
EFFICIENCY_JTH = {
    "S21 XP HYD": 12.0, "S21 XP": 13.5, "S21 PRO": 15.0, "S21+ HYD": 15.0, "S21+": 16.5, "S21 HYD": 16.0, "S21": 17.5,
    "T21": 19.0, "S19 XP HYD": 20.8, "S19 XP": 21.5, "S19K PRO": 23.0, "S19J PRO+": 27.5, "S19J PRO": 29.5,
    "S19 PRO": 29.5, "S19J": 34.5, "S19": 34.0,
}


def efficiency(model: str | None) -> float | None:
    m = (model or "").upper()
    for key in sorted(EFFICIENCY_JTH, key=len, reverse=True):
        if key in m:
            return EFFICIENCY_JTH[key]
    return None


def breakeven(hashprice: float, jth: float, pool_fee_pct: float = 2.0, adder_per_mwh: float = 0.0) -> float:
    """Power price ($/MWh) above which a rig at `jth` J/TH loses money."""
    return hashprice * (1 - pool_fee_pct / 100) / (jth * 0.024) - adder_per_mwh


def hashprice_from_mempool(hashrate: dict, rewards: dict, prices: dict) -> float:
    """Expected $ per PH/s per day at the current difficulty (same method as LoadSettle's daily figure)."""
    difficulty = float(hashrate["currentDifficulty"])
    blocks = int(rewards["endBlock"]) - int(rewards["startBlock"]) + 1
    block_btc = int(rewards["totalReward"]) / blocks / 1e8
    return 86_400 * 1e15 / (difficulty * 2**32) * block_btc * float(prices["USD"])


@dataclass
class PriceConf:
    zone: str
    breakeven: float | None = None          # fixed $/MWh for every miner instead of working it out
    efficiency_j_th: float | None = None    # for miners whose model isn't recognised (or set per miner)
    pool_fee_pct: float = 2.0
    adder_per_mwh: float = 0.0              # charges paid per MWh on top of the wholesale price
    hashprice: float | None = None          # fixed $/PH/day instead of the daily figure
    action: str = "sleep"                   # sleep, or low (low-power mode, where the firmware supports it)
    stale_minutes: int = 60                 # a price older than this (from its interval start) isn't acted on
    when_stale: str = "run"                 # run (keep mining) or sleep, while there's no usable price
    refresh_seconds: int = 300              # how often to fetch the price (it changes every 15 minutes)
    live_url: str = LIVE_URL
    hashprice_url: str = HASHPRICE_URL

    def __post_init__(self):
        if self.zone not in ZONES:
            raise ValueError(f"price.zone must be one of {', '.join(ZONES)}")
        if self.action not in ("sleep", "low"):
            raise ValueError("price.action must be sleep or low")
        if self.when_stale not in ("run", "sleep"):
            raise ValueError("price.when_stale must be run or sleep")
        if not 0 <= self.pool_fee_pct < 100:
            raise ValueError("price.pool_fee_pct must be 0-100")


def conf_from_dict(d: dict) -> PriceConf:
    known = PriceConf.__dataclass_fields__
    unknown = set(d) - set(known)
    if unknown:
        raise ValueError(f"price: unknown option(s) {', '.join(sorted(unknown))}")
    return PriceConf(**d)


@dataclass
class PriceFeed:
    conf: PriceConf
    client: httpx.AsyncClient | None = None
    price: float | None = None
    price_at: datetime | None = None        # start of the 15-minute interval the price is for
    hashprice: float | None = None
    hashprice_date: str | None = None
    hashprice_source: str | None = None
    error: str | None = None
    _price_fetched: float = field(default=-1e18)
    _hash_fetched: float = field(default=-1e18)

    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT})
        return self.client

    async def refresh(self, force: bool = False) -> None:
        """Fetch the price (every refresh_seconds) and the hashprice (every 6 hours). Never raises."""
        mono = time.monotonic()
        if force or mono - self._price_fetched >= self.conf.refresh_seconds:
            self._price_fetched = mono
            try:
                await self._fetch_price()
                self.error = None
            except Exception as e:  # noqa: BLE001 - keep the last good price; staleness handles the rest
                self.error = f"price: {type(e).__name__}: {e}"
                log.warning("could not fetch the ERCOT price: %s", self.error)
        need_hash = self.conf.breakeven is None and self.conf.hashprice is None
        if need_hash and (force or self.hashprice is None or mono - self._hash_fetched >= HASHPRICE_REFRESH_S):
            self._hash_fetched = mono
            await self._fetch_hashprice()

    async def _fetch_price(self) -> None:
        r = await self._http().get(self.conf.live_url)
        r.raise_for_status()
        prices = r.json()["prices"]
        px, at = prices.get(self.conf.zone), prices.get("at")
        if px is None or at is None:
            raise ValueError(f"no {self.conf.zone} price in the reply")
        self.price, self.price_at = float(px), datetime.fromisoformat(at)

    async def _fetch_hashprice(self) -> None:
        try:
            r = await self._http().get(self.conf.hashprice_url)
            r.raise_for_status()
            latest = r.json()["latest"]
            when = datetime.fromisoformat(latest["at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - when > HASHPRICE_MAX_AGE:
                raise ValueError(f"LoadSettle's hashprice is from {latest['date']}")
            self.hashprice, self.hashprice_date, self.hashprice_source = float(latest["hashprice"]), latest["date"], "loadsettle.com"
            return
        except Exception as e:  # noqa: BLE001 - fall back to mempool.space
            log.warning("LoadSettle hashprice unavailable (%s: %s); trying mempool.space", type(e).__name__, e)
        try:
            get = self._http().get
            parts = [(await get(f"{MEMPOOL_API}/{p}")).json() for p in ("mining/hashrate/3d", "mining/reward-stats/144", "prices")]
            self.hashprice = hashprice_from_mempool(*parts)
            self.hashprice_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.hashprice_source = "mempool.space"
        except Exception as e:  # noqa: BLE001 - keep the last good figure, if any
            log.warning("could not work out the hashprice: %s: %s", type(e).__name__, e)

    # ---------- decisions ----------
    def fresh_price(self, now: datetime) -> float | None:
        if self.price is None or self.price_at is None:
            return None
        if now - self.price_at > timedelta(minutes=self.conf.stale_minutes):
            return None
        return self.price

    def breakeven_for(self, jth: float | None) -> float | None:
        if self.conf.breakeven is not None:
            return float(self.conf.breakeven)
        hp = self.conf.hashprice if self.conf.hashprice is not None else self.hashprice
        if hp is None or not jth:
            return None
        return breakeven(hp, jth, self.conf.pool_fee_pct, self.conf.adder_per_mwh)

    def decide(self, jth: float | None, now: datetime) -> tuple[str, str]:
        """("sleep" | "low" | "normal", reason) for a miner at `jth` J/TH. Reasons start with "price:"."""
        stale_mode = self.conf.action if self.conf.when_stale == "sleep" else "normal"
        px = self.fresh_price(now)
        if px is None:
            return stale_mode, "price: no recent price, " + ("sleeping" if stale_mode != "normal" else "mining")
        be = self.breakeven_for(jth)
        if be is None:
            why = "efficiency unknown (set efficiency_j_th)" if not jth else "hashprice unknown"
            return stale_mode, f"price: {why}, " + ("sleeping" if stale_mode != "normal" else "mining")
        if px > be:
            return self.conf.action, f"price: ${px:.2f} above breakeven ${be:.2f}"
        return "normal", f"price: ${px:.2f} below breakeven ${be:.2f}"

    def status(self, now: datetime) -> dict:
        return {
            "zone": self.conf.zone,
            "price": self.price,
            "price_at": self.price_at.isoformat(timespec="minutes") if self.price_at else None,
            "fresh": self.fresh_price(now) is not None,
            "hashprice": round(self.hashprice, 2) if self.hashprice is not None else self.conf.hashprice,
            "hashprice_date": self.hashprice_date,
            "hashprice_source": self.hashprice_source if self.conf.hashprice is None else "config",
            "fixed_breakeven": self.conf.breakeven,
            "error": self.error,
        }

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

"""The agent loop: every poll interval, compare each miner's mode with the schedule and fix it.

Behaviour:
  * Sleep commands go out to every miner at once (the point is to drop load fast).
  * Wake-ups are staggered `resume_stagger_seconds` apart, so several miners don't start together.
  * Every command is confirmed by read-back (control.Miner.set_mode). A miner that won't confirm
    is retried next tick and flagged in the status.
  * A manual override (force sleep / force run / pause automation) beats the schedule until it expires.
  * If a miner is unreachable it is left alone and retried; the agent never assumes a state it can't read.
  * Savings are estimated from the time each miner actually spent asleep inside a schedule window:
    (hashing watts - idle watts) x hours, and x price_per_kwh if given.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Config, MinerConf
from .control import Miner, MinerState

log = logging.getLogger("curtail")
IDLE_W = 25.0                       # sleep-mode draw (control board + fans), rough
DEFAULT_W = 3500.0                  # used when neither config nor model gives a figure
# Nameplate watts at stock settings (manufacturer spec sheets). Matched by substring, longest first.
NAMEPLATE_W = {
    "S21 XP HYD": 5676, "S21 XP": 3645, "S21 PRO": 3510, "S21+": 3564, "S21 HYD": 5360, "S21": 3500,
    "T21": 3610, "S19 XP": 3010, "S19K PRO": 2760, "S19J PRO+": 3355, "S19J PRO": 3068, "S19 PRO": 3250,
    "S19J": 3100, "S19": 3250,
}


@dataclass
class Override:
    mode: str | None                # "sleep" | "normal" | None (= pause automation, touch nothing)
    until: float                    # unix time


@dataclass
class MinerSlot:
    conf: MinerConf
    miner: Miner | None = None
    state: MinerState | None = None
    desired: str = "normal"
    reason: str = ""
    last_result: dict | None = None
    failures: int = 0
    asleep_since: float | None = None   # while asleep under the schedule, for savings


@dataclass
class Agent:
    cfg: Config
    slots: list[MinerSlot] = field(default_factory=list)
    override: Override | None = None
    _last_resume: float = 0.0
    db: sqlite3.Connection | None = None

    def __post_init__(self):
        self.slots = [MinerSlot(m) for m in self.cfg.miners]
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.cfg.data_dir / "curtail.db")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (ts REAL, miner TEXT, kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS savings (miner TEXT, start REAL, end REAL, kwh REAL);
        """)

    # ---------- helpers ----------
    def _event(self, miner: str, kind: str, detail: dict | str = "") -> None:
        d = detail if isinstance(detail, str) else json.dumps(detail)
        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (time.time(), miner, kind, d))
        self.db.commit()
        log.info("%s %s %s", miner, kind, d)

    def _watts(self, slot: MinerSlot) -> float:
        if slot.conf.watts:
            return float(slot.conf.watts)
        model = (slot.state.model if slot.state and slot.state.model else "").upper()
        for key in sorted(NAMEPLATE_W, key=len, reverse=True):
            if key in model:
                return float(NAMEPLATE_W[key])
        return DEFAULT_W

    def _close_sleep(self, slot: MinerSlot, now: float) -> None:
        if slot.asleep_since is None:
            return
        hours = (now - slot.asleep_since) / 3600
        kwh = max(0.0, self._watts(slot) - IDLE_W) / 1000 * hours
        self.db.execute("INSERT INTO savings VALUES (?, ?, ?, ?)", (slot.conf.name or slot.conf.ip, slot.asleep_since, now, kwh))
        self.db.commit()
        slot.asleep_since = None

    def desired_mode(self, now: datetime) -> tuple[str | None, str]:
        if self.override and self.override.until > now.timestamp():
            if self.override.mode is None:
                return None, "automation paused"
            return self.override.mode, "manual override"
        self.override = None
        mode, rule = self.cfg.schedule.decide(now)
        return mode, (f"rule: {rule.name}" if rule else "outside schedule")

    # ---------- one tick ----------
    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        mode, reason = self.desired_mode(now)
        await asyncio.gather(*(self._tick_slot(s, mode, reason, now) for s in self.slots))

    async def _tick_slot(self, slot: MinerSlot, mode: str | None, reason: str, now: datetime) -> None:
        name = slot.conf.name or slot.conf.ip
        if slot.miner is None:
            try:
                slot.miner = await Miner.connect(slot.conf.ip, name=name, password=slot.conf.password,
                                                 http_port=slot.conf.http_port, rpc_port=slot.conf.rpc_port)
                self._event(name, "connected", {"model": getattr(slot.miner.m, "model", None)})
            except Exception as e:  # noqa: BLE001
                slot.state = MinerState(False, error=f"{type(e).__name__}: {e}")
                return
        slot.state = await slot.miner.read()
        slot.reason = reason
        if mode is None or not slot.state.reachable:
            return
        slot.desired = mode
        # Savings bookkeeping: count time asleep while the schedule (not an override) wants sleep.
        ts = now.timestamp()
        scheduled_sleep = reason.startswith("rule:") and mode == "sleep"
        if slot.state.mode == "sleep" and scheduled_sleep and slot.asleep_since is None:
            slot.asleep_since = ts
        elif not (slot.state.mode == "sleep" and scheduled_sleep):
            self._close_sleep(slot, ts)

        if slot.state.mode == mode:
            slot.failures = 0
            return
        if mode == "normal" and slot.state.mode in ("sleep", "low"):
            # Stagger wake-ups across miners.
            if time.monotonic() - self._last_resume < self.cfg.resume_stagger_seconds:
                return
            self._last_resume = time.monotonic()
        res = await slot.miner.set_mode(mode, confirm_timeout_s=self.cfg.confirm_timeout_seconds)
        slot.last_result = {"ok": res.ok, "mode": mode, "method": res.method, "attempts": res.attempts,
                            "seconds": round(res.seconds, 1), "error": res.error}
        self._event(name, "set_mode", slot.last_result)
        slot.failures = 0 if res.ok else slot.failures + 1
        if res.ok and mode == "sleep" and scheduled_sleep:
            slot.asleep_since = ts

    # ---------- status ----------
    def savings(self) -> dict:
        rows = self.db.execute("SELECT miner, SUM(kwh) FROM savings GROUP BY miner").fetchall()
        total = sum(k or 0 for _, k in rows)
        out = {"kwh_total": round(total, 2), "by_miner": {m: round(k or 0, 2) for m, k in rows}}
        if self.cfg.price_per_kwh:
            out["dollars_total"] = round(total * float(self.cfg.price_per_kwh), 2)
        return out

    def status(self) -> dict:
        now = datetime.now(timezone.utc)
        mode, reason = self.desired_mode(now)
        nxt = self.cfg.schedule.next_change(now)
        return {
            "now": now.astimezone(self.cfg.schedule.tz).isoformat(timespec="minutes"),
            "desired": mode, "reason": reason,
            "next_change": nxt.astimezone(self.cfg.schedule.tz).isoformat(timespec="minutes") if nxt else None,
            "override": ({"mode": self.override.mode,
                          "until": datetime.fromtimestamp(self.override.until, self.cfg.schedule.tz).isoformat(timespec="minutes")}
                         if self.override else None),
            "miners": [{
                "name": s.conf.name or s.conf.ip, "ip": s.conf.ip,
                "reachable": bool(s.state and s.state.reachable),
                "mode": s.state.mode if s.state else "unknown",
                "hashrate": s.state.hashrate if s.state else None,
                "unit": s.state.unit if s.state else None,
                "model": s.state.model if s.state else None,
                "error": s.state.error if s.state else None,
                "last_command": s.last_result, "failing": s.failures > 0,
            } for s in self.slots],
            "savings": self.savings(),
        }

    def set_override(self, mode: str | None, minutes: int) -> None:
        self.override = Override(mode, time.time() + minutes * 60)
        self._event("*", "override", {"mode": mode, "minutes": minutes})

    def clear_override(self) -> None:
        self.override = None
        self._event("*", "override", "cleared")

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - keep the loop alive no matter what
                log.exception("tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.poll_seconds)
            except asyncio.TimeoutError:
                pass

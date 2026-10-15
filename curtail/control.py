"""Talk to one miner: read its state, change its mode, and prove the change happened.

Control goes through pyasic (Apache-2.0), which covers stock Bitmain, Braiins OS, LuxOS, Vnish,
Whatsminer, Avalon and more. pyasic's stop/resume calls report success without checking, and some
stock Antminer firmware accepts a mode change but ignores it. So every command here is followed by
a read-back, and stock Antminers get two fallback encodings before we give up:

  1. pyasic's default: {"miner-mode": 1}
  2. the mode as a string: {"miner-mode": "1"}           (some firmware only accepts strings)
  3. the key the firmware reports: {"bitmain-work-mode": "1"}

The encoding that worked is remembered per miner and tried first next time.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from pyasic import settings
from pyasic.config import MiningModeConfig

MODES = ("normal", "sleep", "low")
MODE_TO_INT = {"normal": 0, "sleep": 1, "low": 3}


@dataclass
class MinerState:
    reachable: bool
    mode: str = "unknown"              # normal | sleep | low | unknown
    hashrate: float | None = None      # in the miner's own unit (TH/s for SHA-256, GH/s for Scrypt...)
    unit: str | None = None
    model: str | None = None
    firmware: str | None = None
    error: str | None = None


@dataclass
class ApplyResult:
    ok: bool
    mode: str
    method: str | None = None          # which encoding worked
    attempts: list[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None


class Miner:
    """A connected miner. Build with `await Miner.connect(ip)` (auto-detect via pyasic)."""

    def __init__(self, ip: str, pyasic_miner, name: str | None = None):
        self.ip = ip
        self.name = name or ip
        self.m = pyasic_miner
        self.method: str | None = None   # remembered working encoding

    @classmethod
    async def connect(cls, ip: str, name: str | None = None, password: str | None = None,
                      http_port: int | None = None, rpc_port: int | None = None) -> "Miner":
        """Detect the miner type with pyasic. `http_port`/`rpc_port` are for the simulator in tests."""
        if http_port or rpc_port:
            pm = await _connect_explicit(ip, http_port, rpc_port)
        else:
            from pyasic import get_miner

            pm = await get_miner(ip)
            if pm is None:
                raise ConnectionError(f"{ip}: no supported miner found")
        if password and getattr(pm, "web", None) is not None and hasattr(pm.web, "pwd"):
            pm.web.pwd = password
        return cls(ip, pm, name)

    @property
    def is_stock_antminer(self) -> bool:
        from pyasic.miners.backends.antminer import AntminerModern

        return isinstance(self.m, AntminerModern)

    async def read(self) -> MinerState:
        try:
            cfg = await asyncio.wait_for(self.m.get_config(), timeout=10)
            mode = getattr(cfg.mining_mode, "mode", "unknown")
            if mode not in MODES:
                # Firmware without explicit modes (or pyasic's "default"): infer from is_mining.
                mining = await asyncio.wait_for(self.m.is_mining(), timeout=10)
                mode = "normal" if mining else ("sleep" if mining is False else "unknown")
            hr = await asyncio.wait_for(self.m.get_hashrate(), timeout=10)
            rate = float(hr.rate) if hr is not None and hasattr(hr, "rate") else (float(hr) if hr is not None else None)
            unit = str(hr.unit) if hr is not None and hasattr(hr, "unit") else None
            return MinerState(True, mode, rate, unit, getattr(self.m, "model", None), getattr(self.m, "fw_ver", None))
        except Exception as e:  # noqa: BLE001 - any failure means "can't trust what we see"
            return MinerState(False, error=f"{type(e).__name__}: {e}")

    async def read_mode(self) -> str:
        return (await self.read()).mode

    # ---------- commands ----------
    async def _send(self, mode: str, method: str) -> None:
        if method == "pyasic":
            if mode == "sleep":
                await self.m.stop_mining()
            elif mode == "normal":
                await self.m.resume_mining()
            else:
                cfg = await self.m.get_config()
                cfg.mining_mode = MiningModeConfig.low()
                await self.m.send_config(cfg)
        elif method == "string":
            prev = settings.get("antminer_mining_mode_as_str", False)
            settings.update("antminer_mining_mode_as_str", True)
            try:
                await self._send(mode, "pyasic")
            finally:
                settings.update("antminer_mining_mode_as_str", prev)
        elif method == "work-mode-key":
            cfg = await self.m.get_config()
            body = cfg.as_am_modern()
            body.pop("miner-mode", None)
            body["bitmain-work-mode"] = str(MODE_TO_INT[mode])
            await self.m.web.set_miner_conf(body)
        else:
            raise ValueError(method)

    def _methods(self) -> list[str]:
        order = ["pyasic", "string", "work-mode-key"] if self.is_stock_antminer else ["pyasic"]
        if self.method in order:
            order.remove(self.method)
            order.insert(0, self.method)
        return order

    async def set_mode(self, mode: str, confirm_timeout_s: float = 30.0, poll_s: float = 2.0) -> ApplyResult:
        """Send `mode` and wait until the miner reports it. Tries each encoding in turn."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        t0 = time.monotonic()
        res = ApplyResult(False, mode)
        for method in self._methods():
            res.attempts.append(method)
            try:
                await asyncio.wait_for(self._send(mode, method), timeout=20)
            except Exception as e:  # noqa: BLE001
                res.error = f"{method}: {type(e).__name__}: {e}"
                continue
            deadline = time.monotonic() + confirm_timeout_s
            while time.monotonic() < deadline:
                if await self.read_mode() == mode:
                    self.method = method
                    res.ok, res.method = True, method
                    res.seconds = time.monotonic() - t0
                    res.error = None
                    return res
                await asyncio.sleep(poll_s)
            res.error = f"{method}: miner accepted the command but still reports a different mode"
        res.seconds = time.monotonic() - t0
        return res

    async def wait_hashrate(self, predicate, timeout_s: float, poll_s: float = 5.0) -> tuple[bool, float | None, float]:
        """Poll hashrate until predicate(rate) is true (rate in the miner's own unit).
        Returns (ok, last_rate, seconds)."""
        t0 = time.monotonic()
        last = None
        while True:
            st = await self.read()
            last = st.hashrate
            if last is not None and predicate(last):
                return True, last, time.monotonic() - t0
            if time.monotonic() - t0 >= timeout_s:
                return False, last, time.monotonic() - t0
            await asyncio.sleep(poll_s)


async def _connect_explicit(ip: str, http_port: int | None, rpc_port: int | None):
    """Build a stock-Antminer pyasic object on non-standard ports (simulator)."""
    import httpx

    from pyasic.miners.factory import MINER_CLASSES, MinerTypes

    auth = httpx.DigestAuth("root", settings.get("default_antminer_web_password", "root"))
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://{ip}:{http_port}/cgi-bin/get_system_info.cgi", auth=auth, timeout=5)
        model = r.json()["minertype"].upper()
    cls = MINER_CLASSES[MinerTypes.ANTMINER].get(model) or MINER_CLASSES[MinerTypes.ANTMINER][None]
    pm = cls(ip)
    if http_port:
        pm.web.port = http_port
    if rpc_port:
        pm.rpc.port = rpc_port
    return pm

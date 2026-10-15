"""A simulated stock-firmware Bitmain Antminer (S19/S21 generation), for tests and development.

It speaks the same two interfaces pyasic and the stock web UI use:

  * HTTP with Digest auth (realm "antMiner Configuration") on `http_port`:
      GET  /                              401 challenge (how pyasic recognizes an Antminer)
      GET  /cgi-bin/get_system_info.cgi   model, firmware, MAC
      GET  /cgi-bin/get_miner_conf.cgi    config incl. "bitmain-work-mode" (0 normal, 1 sleep, 3 low)
      POST /cgi-bin/set_miner_conf.cgi    JSON config; the mode is written as "miner-mode"
      GET  /cgi-bin/summary.cgi           status list
  * The cgminer-style JSON socket API on `rpc_port` (normally 4028): summary, stats, version.

Mining physics are simplified: after a mode change the hashrate moves toward its target over
`ramp_s` seconds (sleep -> 0 after `sleep_delay_s`), and power follows hashrate plus control-board
and fan overhead.

Knobs reproduce real-world quirks so the agent's verification logic can be tested:
  ignore_mode_writes  accept set_miner_conf ("M000 OK") but never change mode (reported on stock S19)
  mode_key            which key the firmware honours when writing: "miner-mode" (default),
                      "bitmain-work-mode", or "either"
  mode_as_str         firmware only honours the mode when it's sent as a string ("1" not 1)
  offline             refuse connections (unreachable miner)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from aiohttp import web

REALM = "antMiner Configuration"
MODES = {0: "normal", 1: "sleep", 3: "low"}


@dataclass
class SimAntminer:
    model: str = "Antminer S21 XP"
    rated_ths: float = 270.0
    rated_w: float = 3645.0
    idle_w: float = 25.0                # control board + fans idling in sleep
    password: str = "root"
    ramp_s: float = 2.0                 # real units take minutes; tests keep this short
    sleep_delay_s: float = 0.5
    mode_key: str = "miner-mode"
    mode_as_str: bool = False
    ignore_mode_writes: bool = False
    offline: bool = False
    firmware: str = "Antminer-S21-XP-release-20250312"
    mac: str = "02:00:00:aa:bb:cc"

    # state
    mode: int = 0
    _from_ths: float = field(default=270.0, repr=False)
    _target_ths: float = field(default=270.0, repr=False)
    _changed_at: float = field(default_factory=time.monotonic, repr=False)
    writes: list[dict] = field(default_factory=list, repr=False)
    conf_extra: dict = field(default_factory=lambda: {
        "pools": [{"url": "stratum+tcp://pool.example.com:3333", "user": "worker.001", "pass": "x"}],
        "api-listen": True, "api-network": True, "api-groups": "A:stats:pools:devs:summary:version",
        "api-allow": "A:0/0,W:*", "bitmain-fan-ctrl": False, "bitmain-fan-pwm": "100",
        "bitmain-use-vil": True, "bitmain-freq": "0", "bitmain-voltage": "1500",
        "bitmain-ccdelay": "0", "bitmain-pwth": "0", "freq-level": "100",
    }, repr=False)

    # ---------- physics ----------
    def _target_for(self, mode: int) -> float:
        return {0: self.rated_ths, 1: 0.0, 3: self.rated_ths * 0.7}.get(mode, self.rated_ths)

    def set_mode(self, mode: int) -> None:
        if mode == self.mode:
            return
        self._from_ths = self.hashrate_ths()
        self.mode = mode
        self._target_ths = self._target_for(mode)
        self._changed_at = time.monotonic()

    def hashrate_ths(self) -> float:
        dt = time.monotonic() - self._changed_at
        if self._target_ths == 0.0:
            return 0.0 if dt >= self.sleep_delay_s else self._from_ths
        frac = min(1.0, dt / self.ramp_s) if self.ramp_s > 0 else 1.0
        return self._from_ths + (self._target_ths - self._from_ths) * frac

    def power_w(self) -> float:
        return self.idle_w + (self.rated_w - self.idle_w) * (self.hashrate_ths() / self.rated_ths)

    # ---------- config handling ----------
    def get_conf(self) -> dict:
        return {**self.conf_extra, "bitmain-work-mode": str(self.mode)}

    def apply_conf(self, body: dict) -> None:
        self.writes.append(body)
        for k in ("pools", "bitmain-fan-ctrl", "bitmain-fan-pwm", "freq-level"):
            if k in body:
                self.conf_extra[k] = body[k]
        if self.ignore_mode_writes:
            return
        keys = {"miner-mode": ["miner-mode"], "bitmain-work-mode": ["bitmain-work-mode"],
                "either": ["miner-mode", "bitmain-work-mode"]}[self.mode_key]
        for k in keys:
            if k not in body:
                continue
            v = body[k]
            if self.mode_as_str and not isinstance(v, str):
                continue
            try:
                m = int(v)
            except (TypeError, ValueError):
                continue
            if m in MODES:
                self.set_mode(m)
                return


# ---------- Digest auth (RFC 2617, MD5, qop=auth) ----------

def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _parse_digest(header: str) -> dict:
    out = {}
    for part in header[len("Digest "):].split(","):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v.strip('"')
    return out


class _Auth:
    def __init__(self, user: str, password: str):
        self.user, self.password = user, password
        self.nonces: set[str] = set()

    def challenge(self) -> web.Response:
        nonce = os.urandom(8).hex()
        self.nonces.add(nonce)
        return web.Response(status=401, headers={
            "WWW-Authenticate": f'Digest realm="{REALM}", nonce="{nonce}", qop="auth"'})

    def ok(self, request: web.Request) -> bool:
        h = request.headers.get("Authorization", "")
        if not h.startswith("Digest "):
            return False
        p = _parse_digest(h)
        if p.get("username") != self.user or p.get("nonce") not in self.nonces:
            return False
        ha1 = _md5(f"{self.user}:{REALM}:{self.password}")
        ha2 = _md5(f"{request.method}:{p.get('uri', '')}")
        expect = _md5(f"{ha1}:{p['nonce']}:{p.get('nc', '')}:{p.get('cnonce', '')}:{p.get('qop', '')}:{ha2}")
        return expect == p.get("response")


# ---------- servers ----------

class SimServer:
    """Runs one SimAntminer's HTTP and RPC servers on localhost."""

    def __init__(self, miner: SimAntminer, http_port: int = 0, rpc_port: int = 0, host: str = "127.0.0.1"):
        self.m, self.host = miner, host
        self.http_port, self.rpc_port = http_port, rpc_port
        self._auth = _Auth("root", miner.password)
        self._runner: web.AppRunner | None = None
        self._rpc: asyncio.base_events.Server | None = None

    async def start(self) -> "SimServer":
        app = web.Application()
        app.router.add_get("/", self._root)
        app.router.add_route("*", "/cgi-bin/{cmd}.cgi", self._cgi)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.http_port)
        await site.start()
        self.http_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self._rpc = await asyncio.start_server(self._rpc_conn, self.host, self.rpc_port)
        self.rpc_port = self._rpc.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._rpc:
            self._rpc.close()
            await self._rpc.wait_closed()
        if self._runner:
            await self._runner.cleanup()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()

    # --- HTTP ---
    async def _root(self, request: web.Request) -> web.Response:
        if self.m.offline:
            raise web.HTTPServiceUnavailable()
        if not self._auth.ok(request):
            return self._auth.challenge()
        return web.Response(text="<html><title>Antminer</title></html>", content_type="text/html")

    async def _cgi(self, request: web.Request) -> web.Response:
        if self.m.offline:
            raise web.HTTPServiceUnavailable()
        if not self._auth.ok(request):
            return self._auth.challenge()
        cmd = request.match_info["cmd"]
        m = self.m
        if cmd == "get_system_info":
            return web.json_response({"minertype": m.model, "nettype": "DHCP", "netdevice": "eth0",
                                      "macaddr": m.mac, "hostname": "Antminer", "ipaddress": self.host,
                                      "system_mode": "GNU/Linux", "system_kernel_version": "Linux 4.9.113",
                                      "system_filesystem_version": m.firmware, "firmware_type": "Release",
                                      "serinum": "SIMSERIAL0001"})
        if cmd == "get_miner_conf":
            return web.json_response(m.get_conf())
        if cmd == "set_miner_conf":
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return web.json_response({"stats": "error", "code": "M001", "msg": "bad json"})
            m.apply_conf(body)
            return web.json_response({"stats": "success", "code": "M000", "msg": "OK!"})
        if cmd == "summary":
            status = [{"type": "rate", "status": "s", "code": 0, "msg": ""},
                      {"type": "network", "status": "s", "code": 0, "msg": ""},
                      {"type": "fans", "status": "s", "code": 0, "msg": ""},
                      {"type": "temp", "status": "s", "code": 0, "msg": ""}]
            return web.json_response({"STATUS": {"STATUS": "S", "when": int(time.time())},
                                      "SUMMARY": [{"rate_5s": m.hashrate_ths() * 1000, "rate_unit": "GH/s",
                                                   "status": status}]})
        if cmd == "get_blink_status":
            return web.json_response({"blink": False})
        raise web.HTTPNotFound()

    # --- RPC (cgminer socket API) ---
    async def _rpc_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self.m.offline:
                return
            raw = await asyncio.wait_for(reader.read(4096), timeout=5)
            try:
                cmd = json.loads(raw.decode().strip("\x00")).get("command", "")
            except json.JSONDecodeError:
                cmd = raw.decode().strip()
            writer.write((json.dumps(self._rpc_reply(cmd)) + "\x00").encode())
            await writer.drain()
        finally:
            writer.close()

    def _rpc_reply(self, cmd: str) -> dict:
        m = self.m
        ghs = m.hashrate_ths() * 1000
        st = [{"STATUS": "S", "When": int(time.time()), "Code": 11, "Msg": "Summary", "Description": "bmminer 1.0.0"}]
        if cmd == "summary":
            return {"STATUS": st, "SUMMARY": [{"Elapsed": 3600, "GHS 5s": round(ghs, 2), "GHS av": round(ghs, 2),
                                               "Accepted": 100, "Rejected": 0, "Hardware Errors": 0}], "id": 1}
        if cmd == "stats":
            return {"STATUS": st, "STATS": [{"BMMiner": "1.0.0", "Miner": m.firmware, "Type": m.model},
                                            {"Elapsed": 3600, "GHS 5s": round(ghs, 2), "total_rateideal": m.rated_ths * 1000,
                                             "rate_unit": "GH/s", "fan_num": 4, "fan1": 3000, "fan2": 3000,
                                             "fan3": 3000, "fan4": 3000, "chain_power": round(m.power_w())}], "id": 1}
        if cmd == "version":
            return {"STATUS": st, "VERSION": [{"BMMiner": "1.0.0", "API": "3.1", "Miner": m.firmware,
                                               "CompileTime": "Wed Mar 12 2025", "Type": m.model}], "id": 1}
        return {"STATUS": [{"STATUS": "E", "Code": 14, "Msg": "Invalid command"}], "id": 1}

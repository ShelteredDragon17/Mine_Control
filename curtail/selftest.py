"""Self-test one miner: sleep it, prove it stopped, wake it, prove it recovered, put it back.

    curtail selftest 192.168.1.50

What it checks, in order:
  1. The miner is reachable and pyasic recognizes it (model, firmware).
  2. A Sleep command is confirmed by the miner's own config read-back (and which encoding worked).
  3. Hashrate falls to near zero within `sleep_timeout_s` (power follows hashrate on these machines;
     a wall meter is the only way to see watts directly).
  4. A Normal command is confirmed by read-back.
  5. Hashrate starts recovering within `wake_timeout_s` (full speed can take several minutes).
The miner is always returned to the mode it started in, even if a step fails.
The report is printed and saved as JSON so it can be shared for the compatibility list.
"""
from __future__ import annotations

import asyncio
import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .control import Miner


async def run_selftest(ip: str, password: str | None = None, http_port: int | None = None,
                       rpc_port: int | None = None, sleep_timeout_s: float = 180, wake_timeout_s: float = 600,
                       confirm_timeout_s: float = 45, poll_s: float = 5.0, out_dir: Path | None = Path(".")) -> dict:
    rep: dict = {"tool": "loadsettle-curtail", "version": __version__, "ip": ip,
                 "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "host": platform.system(), "steps": [], "passed": False}

    def step(name: str, ok: bool, **kw):
        rep["steps"].append({"step": name, "ok": ok, **kw})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {kw}" if kw else ""))
        return ok

    print(f"Self-test {ip}")
    try:
        m = await Miner.connect(ip, password=password, http_port=http_port, rpc_port=rpc_port)
    except Exception as e:  # noqa: BLE001
        step("connect", False, error=f"{type(e).__name__}: {e}")
        return _finish(rep, out_dir)
    st = await m.read()
    rep.update(model=st.model, firmware=st.firmware, pyasic_class=type(m.m).__name__,
               start_mode=st.mode, baseline_hashrate=st.hashrate, unit=st.unit)
    if not step("read state", st.reachable and st.mode != "unknown", mode=st.mode, hashrate=st.hashrate, unit=st.unit,
                error=st.error):
        return _finish(rep, out_dir)
    start_mode = st.mode
    baseline = st.hashrate or 0.0

    try:
        if start_mode != "normal":
            # Test from a running state so the hashrate drop means something.
            r = await m.set_mode("normal", confirm_timeout_s=confirm_timeout_s)
            if not step("wake first (miner was asleep)", r.ok, method=r.method, error=r.error):
                return _finish(rep, out_dir)
            ok, last, secs = await m.wait_hashrate(lambda h: h > 0, wake_timeout_s, poll_s)
            baseline = last or baseline
            step("hashing before test", ok, hashrate=last, seconds=round(secs))

        r = await m.set_mode("sleep", confirm_timeout_s=confirm_timeout_s)
        rep["sleep_method"] = r.method
        if not step("sleep command confirmed", r.ok, method=r.method, attempts=r.attempts,
                    seconds=round(r.seconds, 1), error=r.error):
            return _finish(rep, out_dir)
        ok, last, secs = await m.wait_hashrate(lambda h: h <= max(0.02 * baseline, 0.0), sleep_timeout_s, poll_s)
        rep["sleep_seconds_to_zero"] = round(secs, 1) if ok else None
        step("hashrate fell to ~0", ok, hashrate=last, seconds=round(secs, 1))

        r = await m.set_mode("normal", confirm_timeout_s=confirm_timeout_s)
        rep["wake_method"] = r.method
        if not step("wake command confirmed", r.ok, method=r.method, attempts=r.attempts,
                    seconds=round(r.seconds, 1), error=r.error):
            return _finish(rep, out_dir)
        ok2, last, secs = await m.wait_hashrate(lambda h: h > 0.1 * baseline if baseline else h > 0, wake_timeout_s, poll_s)
        rep["wake_seconds_to_hashing"] = round(secs, 1) if ok2 else None
        step("hashrate recovering", ok2, hashrate=last, seconds=round(secs, 1))
        rep["passed"] = ok and ok2
    finally:
        try:
            if (await m.read_mode()) != start_mode:
                r = await m.set_mode(start_mode, confirm_timeout_s=confirm_timeout_s)
                step(f"restore original mode ({start_mode})", r.ok, error=r.error)
        except Exception as e:  # noqa: BLE001
            step("restore original mode", False, error=f"{type(e).__name__}: {e}")
    return _finish(rep, out_dir)


def _finish(rep: dict, out_dir: Path | None) -> dict:
    rep["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print("RESULT:", "PASSED" if rep["passed"] else "FAILED")
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        f = out_dir / f"selftest-{rep['ip'].replace(':', '_')}-{int(time.time())}.json"
        f.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print("Report saved:", f)
        rep["report_file"] = str(f)
    return rep


def main_sync(**kw) -> dict:
    return asyncio.run(run_selftest(**kw))

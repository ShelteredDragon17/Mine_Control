"""Agent configuration (YAML). See config.example.yaml for every option."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .schedule import Schedule, schedule_from_config


@dataclass
class MinerConf:
    ip: str
    name: str | None = None
    password: str | None = None        # web password if not the firmware default
    watts: float | None = None         # power when hashing, for the savings estimate (else a model default)
    http_port: int | None = None       # simulator / unusual setups only
    rpc_port: int | None = None


@dataclass
class Config:
    timezone: str
    miners: list[MinerConf]
    schedule: Schedule
    rules_raw: list[dict]
    poll_seconds: int = 60
    resume_stagger_seconds: int = 30          # wake miners one at a time to avoid inrush
    confirm_timeout_seconds: int = 45         # wait for the miner to report the new mode
    price_per_kwh: float | None = None        # on-peak price, for the $ savings estimate
    data_dir: Path = field(default_factory=lambda: Path("data"))
    web_host: str = "127.0.0.1"
    web_port: int = 8787


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tz = raw.get("timezone")
    if not tz:
        raise ValueError("config: 'timezone' is required, e.g. America/Chicago")
    miners = []
    for m in raw.get("miners") or []:
        if isinstance(m, str):
            m = {"ip": m}
        if "ip" not in m:
            raise ValueError(f"config: miner entry without ip: {m}")
        miners.append(MinerConf(**{k: m[k] for k in m if k in MinerConf.__dataclass_fields__}))
    if not miners:
        raise ValueError("config: add at least one miner under 'miners'")
    rules = raw.get("rules") or []
    web = raw.get("web") or {}
    return Config(
        timezone=tz,
        miners=miners,
        schedule=schedule_from_config(tz, rules),
        rules_raw=rules,
        poll_seconds=int(raw.get("poll_seconds", 60)),
        resume_stagger_seconds=int(raw.get("resume_stagger_seconds", 30)),
        confirm_timeout_seconds=int(raw.get("confirm_timeout_seconds", 45)),
        price_per_kwh=raw.get("price_per_kwh"),
        data_dir=Path(raw.get("data_dir", "data")),
        web_host=web.get("host", "127.0.0.1"),
        web_port=int(web.get("port", 8787)),
    )

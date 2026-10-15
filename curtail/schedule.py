"""Curtailment schedule: which rule (if any) says a miner should be asleep at a given moment.

Pure functions, no I/O. A rule is a weekly time window, optionally limited to months or a date range:

    - name: Summer on-peak
      days: [mon, tue, wed, thu, fri]
      start: "16:00"
      end: "21:00"          # end is exclusive; "00:00" or a time before start means "past midnight"
      months: [6, 7, 8, 9]  # optional
      action: sleep         # sleep (default) or low

Times are local wall-clock time in the site's time zone, so a 4-9 pm window stays 4-9 pm across
daylight-saving changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ACTIONS = ("sleep", "low")


@dataclass(frozen=True)
class Rule:
    name: str
    days: tuple[int, ...]            # 0 = Monday
    start: time
    end: time
    action: str = "sleep"
    months: tuple[int, ...] = ()     # empty = every month
    date_from: date | None = None    # inclusive
    date_to: date | None = None      # inclusive

    @property
    def overnight(self) -> bool:
        return self.end <= self.start

    def _applies_on(self, d: date) -> bool:
        if d.weekday() not in self.days:
            return False
        if self.months and d.month not in self.months:
            return False
        if self.date_from and d < self.date_from:
            return False
        if self.date_to and d > self.date_to:
            return False
        return True

    def active(self, local: datetime) -> bool:
        """True if `local` (aware, site time zone) falls inside this rule's window."""
        t = local.time()
        today = local.date()
        if not self.overnight:
            return self._applies_on(today) and self.start <= t < self.end
        # Overnight window: the part after `start` belongs to today's rule, the part before `end`
        # belongs to the previous day's rule.
        if t >= self.start and self._applies_on(today):
            return True
        return t < self.end and self._applies_on(today - timedelta(days=1))


@dataclass
class Schedule:
    tz: ZoneInfo
    rules: list[Rule] = field(default_factory=list)

    def decide(self, now: datetime) -> tuple[str, Rule | None]:
        """Desired mode at `now`: ("sleep" | "low" | "normal", the rule responsible).

        If several rules overlap, the strongest action wins (sleep beats low)."""
        local = now.astimezone(self.tz)
        hits = [r for r in self.rules if r.active(local)]
        if not hits:
            return "normal", None
        hits.sort(key=lambda r: ACTIONS.index(r.action))
        return hits[0].action, hits[0]

    def next_change(self, now: datetime, horizon_hours: int = 24 * 8) -> datetime | None:
        """First minute after `now` at which decide() changes (for display). None if never within horizon."""
        current = self.decide(now)[0]
        t = now.replace(second=0, microsecond=0)
        for _ in range(horizon_hours * 60):
            t += timedelta(minutes=1)
            if self.decide(t)[0] != current:
                return t
        return None


def _parse_time(s: str) -> time:
    hh, mm = str(s).split(":")
    h, m = int(hh), int(mm)
    if h == 24 and m == 0:
        return time(0, 0)
    return time(h, m)


def _parse_days(days) -> tuple[int, ...]:
    if days in (None, "all", "every day", "daily"):
        return tuple(range(7))
    if days in ("weekdays", "weekday"):
        return tuple(range(5))
    if days in ("weekends", "weekend"):
        return (5, 6)
    if isinstance(days, str):
        days = days.replace(",", " ").split()      # "mon" or "mon, wed, fri"
    out = []
    for d in days:
        key = str(d).strip().lower()[:3]
        if key not in DAYS:
            raise ValueError(f"unknown day {d!r} (use mon..sun, weekdays, weekends or all)")
        out.append(DAYS.index(key))
    return tuple(sorted(set(out)))


def rule_from_dict(d: dict) -> Rule:
    action = d.get("action", "sleep")
    if action not in ACTIONS:
        raise ValueError(f"rule {d.get('name')!r}: action must be one of {ACTIONS}")
    months = tuple(int(m) for m in d.get("months", []) or [])
    if any(not 1 <= m <= 12 for m in months):
        raise ValueError(f"rule {d.get('name')!r}: months must be 1-12")
    start, end = _parse_time(d["start"]), _parse_time(d["end"])
    if start == end:
        raise ValueError(f"rule {d.get('name')!r}: start and end are the same")
    return Rule(
        name=str(d.get("name") or f"{d['start']}-{d['end']}"),
        days=_parse_days(d.get("days", "all")),
        start=start,
        end=end,
        action=action,
        months=months,
        date_from=date.fromisoformat(str(d["from"])) if d.get("from") else None,
        date_to=date.fromisoformat(str(d["to"])) if d.get("to") else None,
    )


def schedule_from_config(tz: str, rules: list[dict]) -> Schedule:
    return Schedule(tz=ZoneInfo(tz), rules=[rule_from_dict(r) for r in rules or []])

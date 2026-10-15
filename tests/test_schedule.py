from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from curtail.schedule import schedule_from_config

CT = ZoneInfo("America/Chicago")


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CT)


def test_weekday_window():
    s = schedule_from_config("America/Chicago", [{"name": "peak", "days": "weekdays", "start": "16:00", "end": "21:00"}])
    assert s.decide(at(2026, 10, 14, 15, 59))[0] == "normal"   # Wednesday
    assert s.decide(at(2026, 10, 14, 16, 0))[0] == "sleep"
    assert s.decide(at(2026, 10, 14, 20, 59))[0] == "sleep"
    assert s.decide(at(2026, 10, 14, 21, 0))[0] == "normal"    # end exclusive
    assert s.decide(at(2026, 10, 17, 17, 0))[0] == "normal"    # Saturday


def test_overnight_belongs_to_start_day():
    s = schedule_from_config("America/Chicago", [{"days": ["fri"], "start": "22:00", "end": "02:00"}])
    assert s.decide(at(2026, 10, 16, 23, 0))[0] == "sleep"     # Friday night
    assert s.decide(at(2026, 10, 17, 1, 30))[0] == "sleep"     # early Saturday = Friday's rule
    assert s.decide(at(2026, 10, 18, 1, 30))[0] == "normal"    # early Sunday: no Saturday rule
    assert s.decide(at(2026, 10, 17, 2, 0))[0] == "normal"


def test_months_and_dates():
    s = schedule_from_config("America/Chicago", [
        {"name": "summer", "start": "15:00", "end": "19:00", "months": [6, 7, 8, 9]},
        {"name": "event", "start": "08:00", "end": "09:00", "from": "2026-12-24", "to": "2026-12-25"},
    ])
    assert s.decide(at(2026, 7, 1, 16))[0] == "sleep"
    assert s.decide(at(2026, 10, 1, 16))[0] == "normal"
    assert s.decide(at(2026, 12, 24, 8, 30))[0] == "sleep"
    assert s.decide(at(2026, 12, 26, 8, 30))[0] == "normal"


def test_sleep_beats_low():
    s = schedule_from_config("America/Chicago", [
        {"name": "shoulder", "start": "14:00", "end": "22:00", "action": "low"},
        {"name": "peak", "start": "17:00", "end": "19:00"},
    ])
    assert s.decide(at(2026, 10, 14, 15))[0] == "low"
    mode, rule = s.decide(at(2026, 10, 14, 18))
    assert (mode, rule.name) == ("sleep", "peak")


def test_dst_keeps_wall_clock():
    # US DST ends 2026-11-01 at 02:00. A 16:00-21:00 window stays at 16:00 local either side.
    s = schedule_from_config("America/Chicago", [{"start": "16:00", "end": "21:00"}])
    for day in (31, 1):
        mo = 10 if day == 31 else 11
        assert s.decide(at(2026, mo, day, 16, 0))[0] == "sleep"
        assert s.decide(at(2026, mo, day, 15, 59))[0] == "normal"
    # And from UTC: 16:00 CDT = 21:00Z, 16:00 CST = 22:00Z
    from datetime import timezone
    assert s.decide(datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc))[0] == "sleep"
    assert s.decide(datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc))[0] == "normal"


def test_next_change():
    s = schedule_from_config("America/Chicago", [{"start": "16:00", "end": "21:00"}])
    assert s.next_change(at(2026, 10, 14, 12, 0, )) == at(2026, 10, 14, 16, 0)
    assert schedule_from_config("UTC", []).next_change(at(2026, 10, 14, 12)) is None


def test_single_day_string_and_bad_input():
    s = schedule_from_config("UTC", [{"days": "mon", "start": "10:00", "end": "11:00"}])
    assert s.rules[0].days == (0,)
    with pytest.raises(ValueError):
        schedule_from_config("UTC", [{"start": "10:00", "end": "10:00"}])
    with pytest.raises(ValueError):
        schedule_from_config("UTC", [{"start": "10:00", "end": "11:00", "action": "off"}])
    with pytest.raises(ValueError):
        schedule_from_config("UTC", [{"days": ["funday"], "start": "10:00", "end": "11:00"}])

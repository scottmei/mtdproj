from datetime import datetime

from busapp.config import TZ
from busapp.timeutil import (day_type, gtfs_to_epoch, local_dt, parse_gtfs_time,
                             service_dates_around)


def test_parse_gtfs_time_past_midnight():
    assert parse_gtfs_time("00:00:00") == 0
    assert parse_gtfs_time("25:10:30") == 25 * 3600 + 10 * 60 + 30
    assert parse_gtfs_time(" 7:05:00") == 7 * 3600 + 300


def test_gtfs_to_epoch_normal_day():
    ts = gtfs_to_epoch("20260925", parse_gtfs_time("18:18:00"))
    assert local_dt(ts) == datetime(2026, 9, 25, 18, 18, tzinfo=TZ)


def test_gtfs_to_epoch_after_midnight_rolls_to_next_day():
    ts = gtfs_to_epoch("20260925", parse_gtfs_time("25:10:00"))
    assert local_dt(ts) == datetime(2026, 9, 26, 1, 10, tzinfo=TZ)


def test_gtfs_to_epoch_on_dst_fall_back_day():
    # 2026-11-01 has 25 hours. GTFS measures from noon-12h, which is 23:00 CDT on Oct 31,
    # so 12:00:00 in the schedule is local noon on Nov 1.
    ts = gtfs_to_epoch("20261101", parse_gtfs_time("12:00:00"))
    assert local_dt(ts).replace(tzinfo=None) == datetime(2026, 11, 1, 12, 0)


def test_day_type():
    assert day_type("20260925") == "weekday"   # Friday
    assert day_type("20260926") == "saturday"
    assert day_type("20260927") == "sunday"


def test_service_dates_around():
    ts = int(datetime(2026, 9, 26, 0, 30, tzinfo=TZ).timestamp())
    assert service_dates_around(ts) == ["20260925", "20260926"]

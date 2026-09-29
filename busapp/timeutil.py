"""Service-day time math.

A GTFS stop time like '25:10:00' means 25h10m after "noon minus 12h" on the
service date (local time). Anchoring on noon keeps the math right on DST days.
"""
from datetime import date, datetime, timedelta

from .config import TZ


def parse_gtfs_time(s: str) -> int:
    """'25:10:30' -> 90630 seconds."""
    h, m, sec = s.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def parse_date(yyyymmdd: str) -> date:
    return date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))


def shift_date(yyyymmdd: str, days: int) -> str:
    return (parse_date(yyyymmdd) + timedelta(days=days)).strftime("%Y%m%d")


def service_day_origin(service_date: str) -> int:
    """Epoch seconds of noon-minus-12h on the service date in local time."""
    d = parse_date(service_date)
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=TZ)
    return int(noon.timestamp()) - 12 * 3600


def gtfs_to_epoch(service_date: str, secs: int) -> int:
    return service_day_origin(service_date) + secs


def local_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def day_type(service_date: str) -> str:
    wd = parse_date(service_date).weekday()
    return "saturday" if wd == 5 else "sunday" if wd == 6 else "weekday"


def service_dates_around(ts: int) -> list[str]:
    """Service dates whose trips could be running at `ts`: yesterday (times > 24:00) and today."""
    today = local_dt(ts).date()
    return [(today - timedelta(days=1)).strftime("%Y%m%d"), today.strftime("%Y%m%d")]

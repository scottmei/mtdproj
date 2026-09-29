"""Fetch and parse MTD's GTFS-RT trip updates feed.

MTD publishes absolute predicted times (no `delay`) and drops stops once the
bus has passed them, so a trip's stop list is "stops still to come".
"""
import dataclasses
import sqlite3
from dataclasses import dataclass, field

import httpx
from google.transit import gtfs_realtime_pb2 as rt

from . import config
from .timeutil import shift_date

StopUpdate = rt.TripUpdate.StopTimeUpdate
TripSR = rt.TripDescriptor.ScheduleRelationship


@dataclass(frozen=True)
class StopRT:
    stop_sequence: int
    stop_id: str
    predicted_ts: int  # departure time if present, else arrival


@dataclass
class TripRT:
    trip_id: str
    start_date: str  # GTFS service date once to_service_dates() has run (MTD's raw value before)
    route_id: str
    vehicle_id: str | None
    canceled: bool = False
    stops: dict[int, StopRT] = field(default_factory=dict)  # by stop_sequence

    @property
    def key(self) -> tuple[str, str]:
        return (self.trip_id, self.start_date)

    @property
    def first_sequence(self) -> int | None:
        return min(self.stops) if self.stops else None


@dataclass
class FeedSnapshot:
    feed_ts: int
    trips: dict[tuple[str, str], TripRT]


def parse_trip_updates(data: bytes) -> FeedSnapshot:
    msg = rt.FeedMessage()
    msg.ParseFromString(data)
    trips: dict[tuple[str, str], TripRT] = {}
    for ent in msg.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        td = tu.trip
        if not td.trip_id:
            continue
        trip = TripRT(
            trip_id=td.trip_id,
            start_date=td.start_date,
            route_id=td.route_id,
            vehicle_id=tu.vehicle.id or None,
            canceled=td.schedule_relationship == TripSR.CANCELED,
        )
        for stu in tu.stop_time_update:
            if stu.schedule_relationship in (StopUpdate.SKIPPED, StopUpdate.NO_DATA):
                continue
            ev = stu.departure if stu.HasField("departure") and stu.departure.time else stu.arrival
            if not ev.time:
                continue
            trip.stops[stu.stop_sequence] = StopRT(stu.stop_sequence, stu.stop_id, int(ev.time))
        trips[trip.key] = trip
    return FeedSnapshot(feed_ts=int(msg.header.timestamp), trips=trips)


def overnight_trips(conn: sqlite3.Connection) -> frozenset[str]:
    """Trips whose first stop is scheduled at or after 24:00 on their service day."""
    return frozenset(r[0] for r in conn.execute(
        "SELECT trip_id FROM stop_times GROUP BY trip_id HAVING MIN(departure_s) >= 86400"))


def to_service_dates(snap: FeedSnapshot, overnight: frozenset[str]) -> FeedSnapshot:
    """Re-key trips by their GTFS service date.

    MTD's `start_date` is the calendar day a trip starts on. For a trip timetabled as
    '24:15:00' on service date D that is D+1 (seen on all 390 such trip runs collected,
    even when the trip is listed before midnight), so pairing it with the timetable as-is
    puts the schedule a day late. Every other trip's start_date already is its service date.
    """
    trips = {}
    for trip in snap.trips.values():
        if trip.trip_id in overnight:
            trip = dataclasses.replace(trip, start_date=shift_date(trip.start_date, -1))
        trips[trip.key] = trip
    return FeedSnapshot(snap.feed_ts, trips)


def fetch_trip_updates(client: httpx.Client | None = None) -> FeedSnapshot:
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        resp = client.get(config.TRIP_UPDATES_URL)
        resp.raise_for_status()
        return parse_trip_updates(resp.content)
    finally:
        if own:
            client.close()

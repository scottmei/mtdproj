"""Background collector: polls GTFS-RT and builds the historical delay tables.

Observed departures are inferred from how MTD's feed behaves: once a bus
passes a stop, that stop disappears from the trip's stop_time_updates. The
last prediction MTD made for the stop, just before it disappeared, is our
observed departure time (capped at the poll time we noticed it was gone).
"""
import logging
import sqlite3
import time
from dataclasses import dataclass

import httpx

from . import config
from .keepawake import prevent_sleep
from .realtime import FeedSnapshot, fetch_trip_updates
from .timeutil import day_type, gtfs_to_epoch, local_dt

log = logging.getLogger("collector")

HORIZON_WINDOW_S = 120  # a snapshot for H must be taken within (H*60 - window, H*60] before arrival


@dataclass(frozen=True)
class Departure:
    trip_id: str
    service_date: str
    stop_sequence: int
    stop_id: str
    route_id: str
    vehicle_id: str | None
    observed_ts: int


@dataclass(frozen=True)
class HorizonSnapshot:
    trip_id: str
    service_date: str
    stop_sequence: int
    horizon_min: int
    made_at: int
    predicted_ts: int


def infer_departures(prev: FeedSnapshot, cur: FeedSnapshot, poll_ts: int) -> list[Departure]:
    """Stops present in `prev` that have dropped out of `cur` have been served."""
    out: list[Departure] = []
    for key, old in prev.trips.items():
        if old.canceled:
            continue
        new = cur.trips.get(key)
        if new is not None and new.canceled:
            continue
        if new is not None and new.stops:
            first = new.first_sequence
            gone = [s for seq, s in old.stops.items() if seq < first]
        else:
            # Trip vanished (or has no remaining stops): accept only stops that were
            # due by now, so a dropped/cancelled trip doesn't produce fake observations.
            gone = [s for s in old.stops.values()
                    if s.predicted_ts <= poll_ts + config.VANISHED_TRIP_SLACK_S]
        for s in gone:
            out.append(Departure(old.trip_id, old.start_date, s.stop_sequence, s.stop_id,
                                 old.route_id, old.vehicle_id, min(s.predicted_ts, poll_ts)))
    return out


def horizon_snapshots(cur: FeedSnapshot, poll_ts: int, seen: set) -> list[HorizonSnapshot]:
    """MTD's prediction as it stood ~H minutes before the (predicted) arrival, once per H."""
    out: list[HorizonSnapshot] = []
    for trip in cur.trips.values():
        if trip.canceled:
            continue
        for s in trip.stops.values():
            horizon = s.predicted_ts - poll_ts
            for h in config.HORIZONS_MIN:
                if h * 60 - HORIZON_WINDOW_S < horizon <= h * 60:
                    k = (trip.trip_id, trip.start_date, s.stop_sequence, h)
                    if k not in seen:
                        seen.add(k)
                        out.append(HorizonSnapshot(trip.trip_id, trip.start_date,
                                                   s.stop_sequence, h, poll_ts, s.predicted_ts))
    return out


class Collector:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.prev: FeedSnapshot | None = None
        self.prev_poll_ts: int | None = None
        self.seen_horizons: set = set()
        # (trip_id, start_date) -> stop_sequences recorded as departed this run; if MTD
        # re-lists one of those stops, the bus hadn't really left and we retract it
        self.observed: dict[tuple[str, str], set[int]] = {}
        self.last_retracted = 0
        self.last_reseed_gap: int | None = None
        self.trip_info = {r["trip_id"]: (r["route_id"], r["direction_id"])
                          for r in conn.execute("SELECT trip_id, route_id, direction_id FROM trips")}
        if not self.trip_info:
            raise RuntimeError("No static GTFS loaded. Run scripts/load_gtfs.py first.")

    def _scheduled(self, trip_id: str, seq: int) -> int | None:
        r = self.conn.execute(
            "SELECT departure_s FROM stop_times WHERE trip_id=? AND stop_sequence=?", (trip_id, seq)
        ).fetchone()
        return r[0] if r else None

    def _observation_rows(self, deps: list[Departure], poll_gap_s: int) -> list[tuple]:
        rows = []
        for d in deps:
            sched_s = self._scheduled(d.trip_id, d.stop_sequence)
            info = self.trip_info.get(d.trip_id)
            if sched_s is None or info is None:
                continue
            sched_ts = gtfs_to_epoch(d.service_date, sched_s)
            delay = d.observed_ts - sched_ts
            if abs(delay) > config.MAX_ABS_DELAY_S:
                continue
            route_id, direction_id = info
            rows.append((d.trip_id, d.service_date, d.stop_sequence, d.stop_id, route_id,
                         direction_id, d.vehicle_id, sched_ts, d.observed_ts, delay,
                         local_dt(sched_ts).hour, day_type(d.service_date), poll_gap_s))
        return rows

    def process(self, cur: FeedSnapshot, poll_ts: int) -> int:
        """Ingest one feed snapshot. Returns the number of new observations written."""
        n_new = 0
        with self.conn:
            self.last_retracted = self._retract_relisted(cur)
            gap = poll_ts - self.prev_poll_ts if self.prev is not None else None
            self.last_reseed_gap = gap if gap is not None and gap > config.MAX_POLL_GAP_S else None
            # After a long gap (sleep, outage, repeated fetch errors) we can't tell when stops
            # dropped out, so this snapshot only re-seeds state instead of producing departures.
            if gap is not None and self.last_reseed_gap is None:
                rows = self._observation_rows(infer_departures(self.prev, cur, poll_ts), gap)
                before = self.conn.total_changes
                self.conn.executemany(
                    "INSERT OR IGNORE INTO observed_departures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                n_new = self.conn.total_changes - before
                for r in rows:
                    self.observed.setdefault((r[0], r[1]), set()).add(r[2])
            snaps = horizon_snapshots(cur, poll_ts, self.seen_horizons)
            self.conn.executemany(
                "INSERT OR IGNORE INTO mtd_predictions VALUES (?,?,?,?,?,?)",
                [(s.trip_id, s.service_date, s.stop_sequence, s.horizon_min, s.made_at, s.predicted_ts)
                 for s in snaps])
            self.conn.execute("INSERT OR REPLACE INTO polls VALUES (?,?,?,?,NULL)",
                              (poll_ts, cur.feed_ts, len(cur.trips), n_new))
        self.prev, self.prev_poll_ts = cur, poll_ts
        self._prune_seen(cur)
        return n_new

    def _retract_relisted(self, cur: FeedSnapshot) -> int:
        """Delete observations for stops that MTD has put back into a trip's remaining stops."""
        doomed = []
        for key, seqs in self.observed.items():
            trip = cur.trips.get(key)
            if trip is None:
                continue
            back = seqs & trip.stops.keys()
            if back:
                seqs -= back
                doomed.extend((key[0], key[1], seq) for seq in back)
        self.conn.executemany(
            "DELETE FROM observed_departures WHERE trip_id=? AND service_date=? AND stop_sequence=?",
            doomed)
        return len(doomed)

    def _prune_seen(self, cur: FeedSnapshot) -> None:
        live = {(t.trip_id, t.start_date) for t in cur.trips.values()}
        self.seen_horizons = {k for k in self.seen_horizons if (k[0], k[1]) in live}
        self.observed = {k: v for k, v in self.observed.items() if k in live}

    def record_error(self, poll_ts: int, err: Exception) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO polls VALUES (?,NULL,NULL,0,?)",
                              (poll_ts, f"{type(err).__name__}: {err}"[:500]))

    def run_forever(self) -> None:
        client = httpx.Client(timeout=10)
        log.info("Collector started, polling %s every %ss", config.TRIP_UPDATES_URL, config.POLL_INTERVAL_S)
        try:
            with prevent_sleep():
                while True:
                    started = time.monotonic()
                    poll_ts = int(time.time())
                    try:
                        snap = fetch_trip_updates(client)
                        n = self.process(snap, poll_ts)
                        if self.last_reseed_gap:
                            log.warning("resumed after %ds without a successful poll; re-seeded state",
                                        self.last_reseed_gap)
                        log.info("poll ok: %d trips, %d new observations%s", len(snap.trips), n,
                                 f", {self.last_retracted} retracted" if self.last_retracted else "")
                    except Exception as e:  # keep collecting through network/feed hiccups
                        log.warning("poll failed: %s", e)
                        self.record_error(poll_ts, e)
                    time.sleep(max(1.0, config.POLL_INTERVAL_S - (time.monotonic() - started)))
        except KeyboardInterrupt:
            log.info("Collector stopped")
        finally:
            client.close()


-- ---------------------------------------------------------------------------
-- Static GTFS (reloaded by scripts/load_gtfs.py)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS stops (
    stop_id   TEXT PRIMARY KEY,   -- boarding point, e.g. 'IT:1'
    base_id   TEXT NOT NULL,      -- stop the rider thinks of, e.g. 'IT'
    stop_code TEXT,
    stop_name TEXT NOT NULL,
    lat       REAL,
    lon       REAL
);
CREATE INDEX IF NOT EXISTS ix_stops_base ON stops(base_id);

CREATE TABLE IF NOT EXISTS routes (
    route_id   TEXT PRIMARY KEY,
    short_name TEXT,
    long_name  TEXT,
    color      TEXT,
    text_color TEXT
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id      TEXT PRIMARY KEY,
    route_id     TEXT NOT NULL,
    service_id   TEXT NOT NULL,
    direction_id INTEGER,
    headsign     TEXT,
    block_id     TEXT
);
CREATE INDEX IF NOT EXISTS ix_trips_service ON trips(service_id);

CREATE TABLE IF NOT EXISTS stop_times (
    trip_id       TEXT    NOT NULL,
    stop_sequence INTEGER NOT NULL,
    stop_id       TEXT    NOT NULL,
    arrival_s     INTEGER NOT NULL,  -- seconds since service-day "midnight" (may exceed 86400)
    departure_s   INTEGER NOT NULL,
    PRIMARY KEY (trip_id, stop_sequence)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_stop_times_stop ON stop_times(stop_id);

-- MTD's calendar.txt is all zeros, so service days come only from calendar_dates.
CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id     TEXT    NOT NULL,
    date           TEXT    NOT NULL,  -- YYYYMMDD
    exception_type INTEGER NOT NULL,  -- 1 = service added, 2 = removed
    PRIMARY KEY (date, service_id)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- Historical data (written by the collector, never truncated)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observed_departures (
    trip_id       TEXT    NOT NULL,
    service_date  TEXT    NOT NULL,  -- YYYYMMDD
    stop_sequence INTEGER NOT NULL,
    stop_id       TEXT    NOT NULL,
    route_id      TEXT    NOT NULL,
    direction_id  INTEGER,
    vehicle_id    TEXT,
    scheduled_ts  INTEGER NOT NULL,  -- epoch seconds
    observed_ts   INTEGER NOT NULL,
    delay_s       INTEGER NOT NULL,  -- observed - scheduled (positive = late)
    hour_local    INTEGER NOT NULL,  -- local hour of scheduled time
    day_type      TEXT    NOT NULL,  -- 'weekday' | 'saturday' | 'sunday'
    poll_gap_s    INTEGER NOT NULL,  -- seconds between the two polls that bracketed this observation
    PRIMARY KEY (trip_id, service_date, stop_sequence)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_obs_model
    ON observed_departures(route_id, direction_id, stop_id, day_type, hour_local);
CREATE INDEX IF NOT EXISTS ix_obs_time ON observed_departures(scheduled_ts);

CREATE TABLE IF NOT EXISTS mtd_predictions (
    trip_id       TEXT    NOT NULL,
    service_date  TEXT    NOT NULL,
    stop_sequence INTEGER NOT NULL,
    horizon_min   INTEGER NOT NULL,  -- MTD prediction as it stood ~H minutes before arrival
    made_at       INTEGER NOT NULL,  -- poll time the snapshot was taken
    predicted_ts  INTEGER NOT NULL,
    PRIMARY KEY (trip_id, service_date, stop_sequence, horizon_min)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS polls (
    poll_ts   INTEGER PRIMARY KEY,
    feed_ts   INTEGER,
    n_trips   INTEGER,
    n_new_obs INTEGER,
    error     TEXT
);

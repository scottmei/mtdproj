"""Central configuration. Values can be overridden via environment variables or a .env file."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TZ = ZoneInfo("America/Chicago")

DB_PATH = Path(os.getenv("BUS_DB_PATH", PROJECT_ROOT / "data" / "bus.db"))
GTFS_DIR = Path(os.getenv("GTFS_DIR", PROJECT_ROOT / "gtfs"))

# MTD REST API v3 (key required, header X-ApiKey)
MTD_API_KEY = os.getenv("MTD_API_KEY", "")
MTD_API_BASE = "https://api.mtd.dev"

# MTD GTFS-RT (no key, protobuf only)
GTFS_RT_BASE = "https://gtfs-rt.mtd.org"
TRIP_UPDATES_URL = f"{GTFS_RT_BASE}/trip-updates"

# Collector
POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "20"))
HORIZONS_MIN = (2, 5, 10, 15, 20, 30)   # MTD prediction snapshots, minutes before arrival
MAX_ABS_DELAY_S = 3600                   # observations beyond this are treated as bad data
MAX_POLL_GAP_S = 90                      # observations after a longer collector gap are low quality
VANISHED_TRIP_SLACK_S = 60

# Prediction model
MIN_SAMPLES = 5
LOOKBACK_DAYS = 28

# Arrivals page
WINDOW_PAST_S = 30 * 60
WINDOW_FUTURE_S = 90 * 60
MAX_ARRIVALS = 20
RT_CACHE_S = 15

WEB_HOST = "127.0.0.1"
WEB_PORT = 8080

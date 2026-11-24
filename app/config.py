"""Settings, read from environment variables or a .env file in the project root."""
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")
# Public URL of the site, used for Steam sign-in (OpenID realm / return URL).
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", str(ROOT / "data" / "tracker.db")))

SECRET_KEY = os.environ.get("SECRET_KEY") or ""
if not SECRET_KEY:
    # Fine for local use; sessions reset on restart. Set SECRET_KEY in production.
    SECRET_KEY = secrets.token_hex(32)
    print("[config] SECRET_KEY not set - using a random one (sign-ins reset on restart).")

# How often every tracked profile gets a playtime snapshot.
SNAPSHOT_INTERVAL_MINUTES = int(os.environ.get("SNAPSHOT_INTERVAL_MINUTES", "60"))
# Store prices older than this are refreshed.
PRICE_MAX_AGE_HOURS = int(os.environ.get("PRICE_MAX_AGE_HOURS", "168"))
# Store regions users can pick for price estimates (Steam country codes).
PRICE_REGIONS = [r.strip().lower() for r in os.environ.get("PRICE_REGIONS", "us,de,gb").split(",") if r.strip()]
DEFAULT_REGION = PRICE_REGIONS[0] if PRICE_REGIONS else "us"
# Stop snapshotting profiles nobody has viewed for this many days (0 = never stop).
INACTIVE_AFTER_DAYS = int(os.environ.get("INACTIVE_AFTER_DAYS", "90"))
# Abuse limits for the public site.
MAX_TRACKED_PROFILES = int(os.environ.get("MAX_TRACKED_PROFILES", "5000"))
NEW_PROFILES_PER_IP_PER_HOUR = int(os.environ.get("NEW_PROFILES_PER_IP_PER_HOUR", "10"))
# Set to 0 to run the web server without the background snapshot/price workers.
RUN_WORKERS = os.environ.get("RUN_WORKERS", "1") != "0"

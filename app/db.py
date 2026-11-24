"""SQLite storage. One connection per thread; WAL mode so workers and web requests coexist."""
import sqlite3
import threading
import time

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    steamid          TEXT PRIMARY KEY,
    persona          TEXT,
    avatar           TEXT,
    profile_url      TEXT,
    added_at         INTEGER NOT NULL,
    last_viewed_at   INTEGER NOT NULL,
    last_snapshot_at INTEGER,
    last_error       TEXT,
    share_spending   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS games (
    appid    INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    img_icon TEXT
);

-- Latest known state per owned game.
CREATE TABLE IF NOT EXISTS user_games (
    steamid           TEXT NOT NULL,
    appid             INTEGER NOT NULL,
    playtime_forever  INTEGER NOT NULL,   -- minutes
    playtime_2weeks   INTEGER NOT NULL DEFAULT 0,
    rtime_last_played INTEGER,
    first_seen        INTEGER NOT NULL,
    PRIMARY KEY (steamid, appid)
);

-- Playtime history. A row is written only when a game's total changes,
-- so the table stays small even with frequent snapshots.
CREATE TABLE IF NOT EXISTS snapshots (
    steamid          TEXT NOT NULL,
    appid            INTEGER NOT NULL,
    ts               INTEGER NOT NULL,
    playtime_forever INTEGER NOT NULL,
    baseline         INTEGER NOT NULL DEFAULT 0  -- 1 = first sighting, not a delta
);
CREATE INDEX IF NOT EXISTS idx_snapshots_user_ts ON snapshots (steamid, ts);

CREATE TABLE IF NOT EXISTS prices (
    appid       INTEGER NOT NULL,
    region      TEXT NOT NULL,
    currency    TEXT,
    initial     INTEGER,   -- regular price in cents
    final       INTEGER,   -- current price in cents
    is_free     INTEGER NOT NULL DEFAULT 0,
    available   INTEGER NOT NULL DEFAULT 1,  -- 0 = not sold in this region / delisted
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (appid, region)
);

-- Imported purchase history (only ever written by the signed-in owner).
CREATE TABLE IF NOT EXISTS purchases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    steamid    TEXT NOT NULL,
    ts         INTEGER,
    date_text  TEXT,
    type       TEXT NOT NULL,      -- purchase | refund | ingame
    total      INTEGER NOT NULL,   -- cents, positive for purchases, negative for refunds
    currency   TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases (steamid);

CREATE TABLE IF NOT EXISTS purchase_items (
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    steamid     TEXT NOT NULL,
    name        TEXT NOT NULL,
    appid       INTEGER,           -- null when it couldn't be matched to an owned game
    amount      INTEGER NOT NULL   -- cents, this item's share of the purchase total
);
CREATE INDEX IF NOT EXISTS idx_purchase_items_user ON purchase_items (steamid);

-- Steam Replay (Year in Review): exact seconds per game per month, 2022 onward.
CREATE TABLE IF NOT EXISTS replay_fetches (
    steamid    TEXT NOT NULL,
    year       INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    has_data   INTEGER NOT NULL,
    PRIMARY KEY (steamid, year)
);
CREATE TABLE IF NOT EXISTS replay_months (
    steamid TEXT NOT NULL,
    month   TEXT NOT NULL,      -- YYYY-MM
    appid   INTEGER NOT NULL,
    seconds INTEGER NOT NULL,
    PRIMARY KEY (steamid, month, appid)
);
CREATE TABLE IF NOT EXISTS replay_games (
    steamid      TEXT NOT NULL,
    appid        INTEGER NOT NULL,
    first_played INTEGER,       -- lifetime first-played timestamp reported by Replay
    PRIMARY KEY (steamid, appid)
);

-- Sessions from the owner's uploaded Steam gameprocess_log.txt.
CREATE TABLE IF NOT EXISTS log_sessions (
    steamid  TEXT NOT NULL,
    appid    INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    PRIMARY KEY (steamid, appid, start_ts)
);
CREATE TABLE IF NOT EXISTS log_coverage (
    steamid  TEXT NOT NULL,
    first_ts INTEGER NOT NULL,  -- span of time a log file covered (gaps-free per file)
    last_ts  INTEGER NOT NULL,
    PRIMARY KEY (steamid, first_ts)
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DATABASE_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


MIGRATIONS = [
    "ALTER TABLE games ADD COLUMN capsule TEXT",  # NULL = not looked up yet, '' = no image
    # Purchases converted to EUR at the purchase date's rate (see fx.py).
    "ALTER TABLE purchases ADD COLUMN code TEXT",
    "ALTER TABLE purchases ADD COLUMN total_eur INTEGER",
    "ALTER TABLE purchase_items ADD COLUMN amount_eur INTEGER",
]


def init() -> None:
    conn().executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn().execute(stmt)
        except sqlite3.OperationalError:
            pass  # already applied
    conn().commit()


def now() -> int:
    return int(time.time())

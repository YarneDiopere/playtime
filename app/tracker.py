"""Playtime snapshots, timeline maths, and the background workers."""
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config, db, history, steam

_user_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
# Synthetic profile created by scripts/seed_demo.py; never fetched from Steam.
DEMO_STEAMID = "76561190000000001"
_requested_regions = {config.DEFAULT_REGION}


# ---------------------------------------------------------------- snapshots

def ensure_user(steamid: str) -> bool:
    """Start tracking a profile. Returns True if it was newly added."""
    c = db.conn()
    if c.execute("SELECT 1 FROM users WHERE steamid=?", (steamid,)).fetchone():
        c.execute("UPDATE users SET last_viewed_at=? WHERE steamid=?", (db.now(), steamid))
        c.commit()
        return False
    c.execute(
        "INSERT INTO users (steamid, added_at, last_viewed_at) VALUES (?,?,?)",
        (steamid, db.now(), db.now()),
    )
    c.commit()
    return True


def snapshot_user(steamid: str) -> None:
    """Fetch the profile's owned games and record any playtime changes."""
    with _user_locks[steamid]:
        c = db.conn()
        user = c.execute("SELECT * FROM users WHERE steamid=?", (steamid,)).fetchone()
        if user is None or steamid == DEMO_STEAMID:
            return
        ts = db.now()
        try:
            summary = steam.player_summary(steamid)
            c.execute(
                "UPDATE users SET persona=?, avatar=?, profile_url=? WHERE steamid=?",
                (summary.get("personaname"), summary.get("avatarfull"), summary.get("profileurl"), steamid),
            )
            games = steam.owned_games(steamid)
        except steam.SteamError as e:
            c.execute("UPDATE users SET last_snapshot_at=?, last_error=? WHERE steamid=?", (ts, str(e), steamid))
            c.commit()
            raise

        first_snapshot = user["last_snapshot_at"] is None or not c.execute(
            "SELECT 1 FROM user_games WHERE steamid=? LIMIT 1", (steamid,)
        ).fetchone()
        existing = {
            r["appid"]: r["playtime_forever"]
            for r in c.execute("SELECT appid, playtime_forever FROM user_games WHERE steamid=?", (steamid,))
        }

        for g in games:
            appid = int(g["appid"])
            pt = int(g.get("playtime_forever", 0))
            c.execute(
                "INSERT INTO games (appid, name, img_icon) VALUES (?,?,?) "
                "ON CONFLICT(appid) DO UPDATE SET name=excluded.name, img_icon=excluded.img_icon",
                (appid, g.get("name") or f"App {appid}", g.get("img_icon_url")),
            )
            prev = existing.get(appid)
            if prev is None:
                c.execute(
                    "INSERT INTO user_games (steamid, appid, playtime_forever, playtime_2weeks, rtime_last_played, first_seen) "
                    "VALUES (?,?,?,?,?,?)",
                    (steamid, appid, pt, g.get("playtime_2weeks", 0), g.get("rtime_last_played"), ts),
                )
                # On the very first snapshot, existing totals are history we can't place on
                # the timeline, so they're stored as a baseline. A game that shows up later
                # with playtime was played since the previous snapshot.
                if pt > 0:
                    c.execute(
                        "INSERT INTO snapshots (steamid, appid, ts, playtime_forever, baseline) VALUES (?,?,?,?,?)",
                        (steamid, appid, ts, pt, 1 if first_snapshot else 0),
                    )
            else:
                if pt != prev:
                    c.execute(
                        "INSERT INTO snapshots (steamid, appid, ts, playtime_forever) VALUES (?,?,?,?)",
                        (steamid, appid, ts, pt),
                    )
                c.execute(
                    "UPDATE user_games SET playtime_forever=?, playtime_2weeks=?, rtime_last_played=? "
                    "WHERE steamid=? AND appid=?",
                    (pt, g.get("playtime_2weeks", 0), g.get("rtime_last_played"), steamid, appid),
                )

        c.execute("UPDATE users SET last_snapshot_at=?, last_error=NULL WHERE steamid=?", (ts, steamid))
        c.commit()
    try:
        history.refresh_replay(steamid)  # cheap after the first run: only missing years are fetched
    except steam.SteamError as e:
        print(f"[replay] {steamid}: {e}")


# ---------------------------------------------------------------- timeline

def timeline(steamid: str, days: int, tz_offset_min: int) -> dict:
    """Minutes played per local day per game.

    tz_offset_min is JavaScript's Date.getTimezoneOffset() (minutes, positive west of UTC).
    Steam updates a game's total when a session ends, so play is attributed to the
    snapshot that first saw it.
    """
    c = db.conn()
    user = c.execute("SELECT added_at FROM users WHERE steamid=?", (steamid,)).fetchone()
    if user is None:
        return {"days": [], "games": {}}

    def local_date(ts: int):
        return (datetime.fromtimestamp(ts, timezone.utc) - timedelta(minutes=tz_offset_min)).date()

    start_day = local_date(user["added_at"])
    per_day, meta = history.backfill(steamid, start_day, tz_offset_min)
    prev: dict[int, int] = {}
    rows = c.execute(
        "SELECT appid, ts, playtime_forever, baseline FROM snapshots WHERE steamid=? ORDER BY appid, ts",
        (steamid,),
    )
    for r in rows:
        appid = r["appid"]
        if r["baseline"]:
            prev[appid] = r["playtime_forever"]
            continue
        delta = r["playtime_forever"] - prev.get(appid, 0)
        prev[appid] = r["playtime_forever"]
        if delta > 0:
            per_day[local_date(r["ts"])][appid]["tracked"] += delta

    today = local_date(db.now())
    earliest = min([d for d, g in per_day.items() if g] or [start_day])
    start = max(min(earliest, start_day), today - timedelta(days=days - 1))
    out_days = []
    d = start
    while d <= today:
        games, src = {}, defaultdict(float)
        for appid, by_src in per_day.get(d, {}).items():
            games[appid] = round(sum(by_src.values()), 1)
            for s, v in by_src.items():
                src[s] += v
        out_days.append({
            "date": d.isoformat(),
            "total": round(sum(games.values()), 1),
            "games": games,
            "src": {s: round(v, 1) for s, v in src.items() if v >= 0.05},
        })
        d += timedelta(days=1)

    appids = {a for day in out_days for a in day["games"]}
    names = {}
    if appids:
        q = ",".join("?" * len(appids))
        names = {r["appid"]: r["name"] for r in c.execute(f"SELECT appid, name FROM games WHERE appid IN ({q})", list(appids))}
    return {"days": out_days, "games": names, "tracking_since": user["added_at"], "history": meta}


# ---------------------------------------------------------------- prices

def request_region(region: str) -> None:
    if region in config.PRICE_REGIONS:
        _requested_regions.add(region)


def _stale_price_batch(region: str, limit: int = 100) -> list[int]:
    cutoff = db.now() - config.PRICE_MAX_AGE_HOURS * 3600
    rows = db.conn().execute(
        "SELECT DISTINCT ug.appid FROM user_games ug "
        "LEFT JOIN prices p ON p.appid = ug.appid AND p.region = ? "
        "WHERE p.appid IS NULL OR p.fetched_at < ? LIMIT ?",
        (region, cutoff, limit),
    )
    return [r["appid"] for r in rows]


def refresh_prices_once() -> int:
    """Fetch one batch of missing/stale prices per requested region. Returns apps updated."""
    c = db.conn()
    updated = 0
    for region in list(_requested_regions):
        appids = _stale_price_batch(region)
        if not appids:
            continue
        result = steam.store_prices(appids, region)
        ts = db.now()
        for appid in appids:
            p = result.get(appid, {"available": False})
            c.execute(
                "INSERT INTO prices (appid, region, currency, initial, final, is_free, available, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(appid, region) DO UPDATE SET "
                "currency=excluded.currency, initial=excluded.initial, final=excluded.final, "
                "is_free=excluded.is_free, available=excluded.available, fetched_at=excluded.fetched_at",
                (appid, region, p.get("currency"), p.get("initial"), p.get("final"),
                 1 if p.get("is_free") else 0, 1 if p.get("available") else 0, ts),
            )
        c.commit()
        updated += len(appids)
        time.sleep(1.5)  # the store API allows roughly 200 requests per 5 minutes
    return updated


# ---------------------------------------------------------------- workers

def _due_users() -> list[str]:
    now = db.now()
    due_before = now - config.SNAPSHOT_INTERVAL_MINUTES * 60
    active_after = now - config.INACTIVE_AFTER_DAYS * 86400 if config.INACTIVE_AFTER_DAYS else 0
    retry_before = now - min(10, config.SNAPSHOT_INTERVAL_MINUTES) * 60  # failed snapshots retry sooner
    rows = db.conn().execute(
        "SELECT steamid FROM users WHERE (last_snapshot_at IS NULL OR last_snapshot_at < ? "
        "OR (last_error IS NOT NULL AND last_snapshot_at < ?)) "
        "AND last_viewed_at >= ? ORDER BY last_snapshot_at IS NOT NULL, last_snapshot_at",
        (due_before, retry_before, active_after),
    )
    return [r["steamid"] for r in rows if r["steamid"] != DEMO_STEAMID]


def _snapshot_loop() -> None:
    while True:
        try:
            for steamid in _due_users():
                try:
                    snapshot_user(steamid)
                except steam.SteamError as e:
                    print(f"[snapshot] {steamid}: {e}")
                time.sleep(1.0)  # stay well under Steam's 100k calls/day
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def refresh_capsules_once() -> int:
    c = db.conn()
    appids = [r["appid"] for r in c.execute("SELECT appid FROM games WHERE capsule IS NULL LIMIT 100")]
    if not appids:
        return 0
    for appid, url in steam.store_capsules(appids).items():
        c.execute("UPDATE games SET capsule=? WHERE appid=?", (url, appid))
    c.commit()
    return len(appids)


def _price_loop() -> None:
    while True:
        try:
            if refresh_capsules_once() + refresh_prices_once():
                continue
        except steam.SteamError as e:
            print(f"[prices] {e}")
            time.sleep(60)
        except Exception:
            traceback.print_exc()
        time.sleep(20)


def start_workers() -> None:
    threading.Thread(target=_snapshot_loop, name="snapshots", daemon=True).start()
    threading.Thread(target=_price_loop, name="prices", daemon=True).start()

"""Playtime from before tracking started.

Three sources, in order of authority:
  replay   - Steam Replay (Year in Review): exact seconds per game per month, 2022 onward.
             Fetched automatically; only available when the profile's Replay is shareable.
  log      - Steam's logs/gameprocess_log.txt, uploaded by the signed-in owner: exact sessions,
             but only for games launched on that PC and only as far back as the log goes.
  estimate - What's left: each game's total at tracking start minus everything placed above,
             spread over the days no source covers, between first and last played.
Whatever still can't be dated (mostly play before 2022) is reported as undated.
"""
import calendar
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import db, steam

REPLAY_FIRST_YEAR = 2022
REFETCH_EMPTY_AFTER = 7 * 86400  # retry years without data (privacy may change; current year appears in December)


# ---------------------------------------------------------------- Steam Replay

def refresh_replay(steamid: str) -> int:
    """Fetch any Replay years not stored yet. Returns the number of years fetched."""
    c = db.conn()
    now = db.now()
    done = {r["year"]: r for r in c.execute("SELECT * FROM replay_fetches WHERE steamid=?", (steamid,))}
    fetched = 0
    for year in range(REPLAY_FIRST_YEAR, datetime.now(timezone.utc).year + 1):
        prev = done.get(year)
        if prev and (prev["has_data"] and year < datetime.now(timezone.utc).year):
            continue
        if prev and now - prev["fetched_at"] < REFETCH_EMPTY_AFTER:
            continue
        stats = steam.year_in_review(steamid, year)
        fetched += 1
        c.execute(
            "INSERT INTO replay_fetches (steamid, year, fetched_at, has_data) VALUES (?,?,?,?) "
            "ON CONFLICT(steamid, year) DO UPDATE SET fetched_at=excluded.fetched_at, has_data=excluded.has_data",
            (steamid, year, now, 1 if stats else 0),
        )
        if stats:
            c.execute("DELETE FROM replay_months WHERE steamid=? AND month LIKE ?", (steamid, f"{year}-%"))
            for m in stats["months"]:
                month = datetime.fromtimestamp(m["rtime_month"], timezone.utc).strftime("%Y-%m")
                for g in m.get("appid") or []:
                    secs = (g.get("stats") or {}).get("total_playtime_seconds") or 0
                    if secs > 0:
                        c.execute(
                            "INSERT OR REPLACE INTO replay_months (steamid, month, appid, seconds) VALUES (?,?,?,?)",
                            (steamid, month, int(g["appid"]), int(secs)),
                        )
            for g in stats.get("game_summary") or []:
                fp = g.get("rtime_first_played_lifetime")
                c.execute(
                    "INSERT INTO replay_games (steamid, appid, first_played) VALUES (?,?,?) "
                    "ON CONFLICT(steamid, appid) DO UPDATE SET first_played="
                    "MIN(COALESCE(replay_games.first_played, excluded.first_played), COALESCE(excluded.first_played, replay_games.first_played))",
                    (steamid, int(g["appid"]), fp or None),
                )
        c.commit()
    return fetched


# ---------------------------------------------------------------- gameprocess_log.txt

LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)$")
ADD_RE = re.compile(r"^AppID (\d+) adding PID (\d+) as a tracked process")
REMOVE_RE = re.compile(r"^AppID (\d+) no longer tracking PID (\d+)")
MAX_SESSION = 24 * 3600  # sessions with a missing end are capped


class LogImportError(Exception):
    pass


def parse_gameprocess_log(text: str, tz_offset_min: int) -> tuple[list[tuple[int, int, int]], tuple[int, int] | None]:
    """Return ([(appid, start_ts, end_ts)], (first_ts, last_ts)).

    Log times are the PC's local time; tz_offset_min is the browser's getTimezoneOffset().
    A session runs from the first tracked process of an app until its last one exits.
    """
    sessions = []
    live: dict[int, set] = defaultdict(set)
    started: dict[int, int] = {}
    first = last = None

    def ts_of(stamp: str) -> int:
        local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(local.timestamp()) + tz_offset_min * 60

    def close(appid: int, end: int) -> None:
        start = started.pop(appid, None)
        live.pop(appid, None)
        if start is not None and end > start:
            sessions.append((appid, start, min(end, start + MAX_SESSION)))

    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        ts = ts_of(m.group(1))
        first = ts if first is None else first
        last = ts
        msg = m.group(2)
        if msg.startswith("Client version"):  # Steam restarted: anything still open ended before this
            for appid in list(started):
                close(appid, ts)
            continue
        a = ADD_RE.match(msg)
        if a:
            appid, pid = int(a.group(1)), a.group(2)
            if not live[appid]:
                started[appid] = ts
            live[appid].add(pid)
            continue
        r = REMOVE_RE.match(msg)
        if r:
            appid, pid = int(r.group(1)), r.group(2)
            live[appid].discard(pid)
            if not live[appid]:
                close(appid, ts)
    for appid in list(started):  # still running when the log was saved
        close(appid, last)
    if first is None:
        return [], None
    return sessions, (first, last)


ACCOUNT_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[[^\]]*\] \[U:1:(\d+)\]")
STEAMID64_BASE = 76561197960265728


def parse_connection_log(text: str, tz_offset_min: int) -> list[tuple[int, int]]:
    """[(ts, account_id)] from Steam's connection_log.txt; tells which account was signed in when."""
    out = []
    for line in text.splitlines():
        m = ACCOUNT_RE.match(line)
        if m:
            local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            out.append((int(local.timestamp()) + tz_offset_min * 60, int(m.group(2))))
    return out


def import_logs(steamid: str, texts: list[str], tz_offset_min: int) -> dict:
    """Add sessions from uploaded log files (re-uploads are de-duplicated).

    gameprocess_log*.txt has the sessions. connection_log*.txt is optional but lets us keep only
    sessions played while *this* account was signed in, for PCs shared by several accounts.
    """
    import bisect

    c = db.conn()
    owned = {r["appid"] for r in c.execute("SELECT appid FROM user_games WHERE steamid=?", (steamid,))}
    my_account = int(steamid) - STEAMID64_BASE
    accounts = sorted(p for t in texts if "[U:1:" in t for p in parse_connection_log(t, tz_offset_min))
    accounts = [p for p in accounts if p[1] != 0]  # 0 = signed out / connecting
    account_ts = [p[0] for p in accounts]

    def signed_in_at(ts: int) -> int | None:
        i = bisect.bisect_right(account_ts, ts) - 1
        return accounts[i][1] if i >= 0 else None

    added = skipped = other_account = files = 0
    for text in texts:
        if "[U:1:" in text:
            continue  # connection log, used above
        sessions, span = parse_gameprocess_log(text, tz_offset_min)
        if span is None:
            continue
        files += 1
        c.execute(
            "INSERT OR REPLACE INTO log_coverage (steamid, first_ts, last_ts) VALUES (?,?,?)",
            (steamid, span[0], span[1]),
        )
        for appid, start, end in sessions:
            if appid not in owned:  # tools, games owned by other accounts on the PC
                skipped += 1
                continue
            if accounts and signed_in_at(start) != my_account:
                other_account += 1
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO log_sessions (steamid, appid, start_ts, end_ts) VALUES (?,?,?,?)",
                (steamid, appid, start, end),
            )
            added += cur.rowcount
    if not files:
        raise LogImportError(
            "That doesn't look like a Steam gameprocess_log.txt. It's in your Steam folder under "
            "logs\\ (usually C:\\Program Files (x86)\\Steam\\logs)."
        )
    c.commit()
    return {
        "files": files,
        "sessions_added": added,
        "sessions_skipped": skipped,
        "sessions_other_account": other_account,
        "account_check": bool(accounts),
    }


def delete_logs(steamid: str) -> None:
    c = db.conn()
    c.execute("DELETE FROM log_sessions WHERE steamid=?", (steamid,))
    c.execute("DELETE FROM log_coverage WHERE steamid=?", (steamid,))
    c.commit()


# ---------------------------------------------------------------- backfill

def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def backfill(steamid: str, start_day: date, tz_offset_min: int) -> tuple[dict, dict]:
    """Minutes per local day per game per source, for days before start_day.

    Returns (per_day, meta) where per_day[date][appid][source] = minutes.
    """
    c = db.conn()
    per_day: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    placed: dict[int, float] = defaultdict(float)
    covered: set[date] = set()

    def local_date(ts: int) -> date:
        return (datetime.fromtimestamp(ts, timezone.utc) - timedelta(minutes=tz_offset_min)).date()

    # 1. Steam Replay: whole years it has data for are fully covered (no row = not played).
    replay_years = sorted(r["year"] for r in c.execute(
        "SELECT year FROM replay_fetches WHERE steamid=? AND has_data=1", (steamid,)))
    for y in replay_years:
        covered.update(d for d in _days(date(y, 1, 1), min(date(y, 12, 31), start_day - timedelta(days=1))))
    for r in c.execute("SELECT month, appid, seconds FROM replay_months WHERE steamid=?", (steamid,)):
        y, m = map(int, r["month"].split("-"))
        month_days = [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]
        before = [d for d in month_days if d < start_day]
        if not before:
            continue
        # A month that straddles tracking start: keep the share of days before it.
        minutes = r["seconds"] / 60 * len(before) / len(month_days)
        for d in before:
            per_day[d][r["appid"]]["replay"] += minutes / len(before)
        placed[r["appid"]] += minutes

    # 2. Uploaded Steam logs, for days Replay doesn't cover.
    log_days: set[date] = set()
    for r in c.execute("SELECT first_ts, last_ts FROM log_coverage WHERE steamid=?", (steamid,)):
        log_days.update(_days(local_date(r["first_ts"]), local_date(r["last_ts"])))
    log_days = {d for d in log_days if d < start_day and d not in covered}
    for r in c.execute("SELECT appid, start_ts, end_ts FROM log_sessions WHERE steamid=?", (steamid,)):
        t = r["start_ts"]
        while t < r["end_ts"]:  # split sessions that cross local midnight
            d = local_date(t)
            next_midnight = int(datetime.combine(d + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()) + tz_offset_min * 60
            seg_end = min(r["end_ts"], next_midnight)
            if d in log_days:
                per_day[d][r["appid"]]["log"] += (seg_end - t) / 60
                placed[r["appid"]] += (seg_end - t) / 60
            t = seg_end
    covered |= log_days

    # 3. Estimate the remainder of each game's total at tracking start.
    baseline = {r["appid"]: r["playtime_forever"] for r in c.execute(
        "SELECT appid, playtime_forever FROM snapshots WHERE steamid=? AND baseline=1", (steamid,))}
    last_played = {r["appid"]: r["rtime_last_played"] for r in c.execute(
        "SELECT appid, rtime_last_played FROM user_games WHERE steamid=?", (steamid,))}
    first_played = {r["appid"]: r["first_played"] for r in c.execute(
        "SELECT appid, first_played FROM replay_games WHERE steamid=?", (steamid,))}
    latest_year = replay_years[-1] if replay_years else None
    latest_rate = {}
    if latest_year:
        days_in_year = 366 if calendar.isleap(latest_year) else 365
        latest_rate = {r["appid"]: r["s"] / 60 / days_in_year for r in c.execute(
            "SELECT appid, SUM(seconds) AS s FROM replay_months WHERE steamid=? AND month LIKE ? GROUP BY appid",
            (steamid, f"{latest_year}-%"))}

    estimated = 0.0
    undated = 0.0
    est_days: set[date] = set()
    gap_start = date(REPLAY_FIRST_YEAR, 1, 1)
    for appid, total in baseline.items():
        remaining = total - placed.get(appid, 0)
        if remaining < 1:
            continue
        if not replay_years:
            undated += remaining  # nothing to anchor an estimate to
            continue
        lp = last_played.get(appid)
        # Steam only reports last-played dates to the API key's owner; otherwise assume
        # the game may have been played right up to tracking start.
        lp_day = min(local_date(lp), start_day - timedelta(days=1)) if lp else start_day - timedelta(days=1)
        fp = first_played.get(appid)
        fp_day = local_date(fp) if fp else None
        if lp_day is None or lp_day < gap_start:
            undated += remaining
            continue
        lo = max(gap_start, fp_day) if fp_day else gap_start
        window = [d for d in _days(lo, lp_day) if d not in covered]
        if not window:
            undated += remaining
            continue
        if fp_day and fp_day >= gap_start:
            share = remaining  # first played in 2022 or later, so none of it is older
        else:
            # Could be from before 2022: assume the same pace as the latest Replay year.
            share = min(remaining, latest_rate.get(appid, 0) * len(window))
        if share >= 1:
            for d in window:
                per_day[d][appid]["estimate"] += share / len(window)
            est_days.update(window)
            estimated += share
        undated += remaining - share

    meta = {
        "replay_years": replay_years,
        "log_range": [min(log_days).isoformat(), max(log_days).isoformat()] if log_days else None,
        "estimate_range": [min(est_days).isoformat(), max(est_days).isoformat()] if est_days else None,
        "estimated_minutes": round(estimated),
        "undated_minutes": round(undated),
    }
    return per_day, meta

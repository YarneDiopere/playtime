"""Build a static, read-only version of the tracker for GitHub Pages.

Runs one tracking cycle (snapshot, Steam Replay, prices, cover art) for the configured
accounts, then writes the site to SITE_DIR: the normal frontend in static mode plus one
JSON file per account. Purchase data is never exported.

Environment:
  STEAM_API_KEY     required for the snapshot
  STATIC_ACCOUNTS   comma-separated SteamID64s
  STATIC_REGION     store region for prices (default de)
  STATIC_TZ         timezone for day boundaries (default Europe/Brussels)
  SITE_DIR          output folder (default site)
"""
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUN_WORKERS", "0")

from app import config, db, steam, tracker  # noqa: E402
from app.web import app  # noqa: E402

ACCOUNTS = [a.strip() for a in os.environ.get("STATIC_ACCOUNTS", "").split(",") if a.strip()]
REGION = os.environ.get("STATIC_REGION", "de")
TZ_NAME = os.environ.get("STATIC_TZ", "Europe/Brussels")
SITE = Path(os.environ.get("SITE_DIR", ROOT / "site"))


def tz_offset_minutes() -> int:
    """JavaScript getTimezoneOffset() convention: minutes *behind* UTC (CET = -60)."""
    try:
        from zoneinfo import ZoneInfo
        off = datetime.now(ZoneInfo(TZ_NAME)).utcoffset()
    except Exception:  # no tz database (e.g. Windows without tzdata): use this machine's zone
        off = datetime.now().astimezone().utcoffset()
    return -int(off.total_seconds() // 60)


def track() -> None:
    db.init()
    for sid in ACCOUNTS:
        tracker.ensure_user(sid)
        try:
            tracker.snapshot_user(sid)
            print(f"snapshot ok: {sid}")
        except steam.SteamError as e:
            print(f"snapshot failed for {sid}: {e}")
    tracker.request_region(REGION)
    for _ in range(30):  # finish pricing / cover art for any new games
        if not (tracker.refresh_capsules_once() + tracker.refresh_prices_once()):
            break


def export() -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "data").mkdir(parents=True)
    client = app.test_client()
    tz = tz_offset_minutes()
    accounts = []
    for sid in ACCOUNTS:
        profile = client.get(f"/api/profile/{sid}?region={REGION}").get_json()
        if "error" in profile:
            print(f"skipping {sid}: {profile['error']}")
            continue
        # Read-only public page: no purchase data, no owner features.
        profile.update(spending=None, can_see_spending=False, is_owner=False, share_spending=False)
        for g in profile["games"]:
            g["paid"] = None
        timeline = client.get(f"/api/profile/{sid}/timeline?days=3650&tz={tz}").get_json()
        (SITE / "data" / f"{sid}.json").write_text(json.dumps(profile, separators=(",", ":")), encoding="utf-8")
        (SITE / "data" / f"{sid}-timeline.json").write_text(json.dumps(timeline, separators=(",", ":")), encoding="utf-8")
        accounts.append({"steamid": sid, "persona": profile["persona"], "avatar": profile["avatar"]})

    site_config = {
        "static": True,
        "accounts": accounts,
        "regions": [REGION],
        "default_region": REGION,
        "snapshot_interval_minutes": 60,
        "built_at": int(datetime.now(timezone.utc).timestamp()),
        "me": None,
    }
    (SITE / "data" / "config.json").write_text(json.dumps(site_config), encoding="utf-8")

    for name in ("app.js", "style.css"):
        shutil.copy(ROOT / "static" / name, SITE / name)
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/style.css"', 'href="style.css"').replace('src="/static/app.js"', 'src="app.js"')
    html = html.replace('<script src="app.js">', '<script>window.PLAYTIME_STATIC = true;</script>\n  <script src="app.js">')
    html = html.replace('<a href="/" class="brand" data-link>', '<a href="./" class="brand">')
    (SITE / "index.html").write_text(html, encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    print(f"site written to {SITE} for {len(accounts)} account(s)")


if __name__ == "__main__":
    if not ACCOUNTS:
        sys.exit("Set STATIC_ACCOUNTS to one or more SteamID64s")
    if "--no-track" not in sys.argv:
        if not config.STEAM_API_KEY:
            sys.exit("STEAM_API_KEY is not set")
        track()
    export()
    db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")  # fold the WAL into tracker.db before it's saved
    db.conn().close()

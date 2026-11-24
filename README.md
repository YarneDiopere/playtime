# Playtime Ledger

A public website that tracks Steam profiles by SteamID and shows **a day-by-day timeline of
time played per game** and **how much was spent** on them.

## How it works

| What | Where it comes from |
|---|---|
| Playtime per game | Steam Web API `GetOwnedGames`. Steam only gives the *current total*, so the server saves a snapshot of every tracked profile every `SNAPSHOT_INTERVAL_MINUTES` (default 60) and the differences become the timeline. The timeline therefore starts on the day a profile is first looked up. |
| Playtime before tracking started | **Steam Replay** (`ISaleFeatureService/GetUserYearInReview`, works with a normal key): exact seconds per game per month from 2022, fetched automatically when the profile's Replay is shareable. **Steam logs** (`logs/gameprocess_log.txt` + `connection_log.txt`), uploaded by the signed-in owner: exact sessions for the months the log covers, filtered to that account. **Estimates** fill the remaining gap (total at tracking start minus everything placed) and are drawn faded. Play from before 2022 is shown as "undated". |
| Library value / estimated cost | Steam store API (`appdetails`, no key needed), fetched in batches of 100 by a background worker, per store region. |
| What you really paid | Steam has **no API** for this. Signed-in owners (Sign in through Steam / OpenID) upload a saved copy of `store.steampowered.com/account/history`. It's private unless the owner ticks "share". |

Profiles must have **Game details** set to Public in Steam's privacy settings.

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # macOS/Linux: .venv/bin/pip
copy .env.example .env                              # then put your key in .env
.venv\Scripts\python run.py
```

Open http://localhost:8000. Get a Steam Web API key at https://steamcommunity.com/dev/apikey.

**No key yet?** `python scripts/seed_demo.py` creates a synthetic demo profile at
http://localhost:8000/u/76561190000000001.

Tests: `python -m unittest discover tests`

## Deploying publicly

- Set `BASE_URL` to the public https address (Steam sign-in returns there), a fixed
  `SECRET_KEY`, and `TRUST_PROXY=1` behind nginx/Caddy.
- Run one process only (the snapshot workers live inside it). SQLite is fine for thousands of profiles.
- Steam allows ~100,000 Web API calls per day. Each snapshot uses 2 calls, so hourly
  snapshots cover roughly 2,000 profiles; raise the interval or `MAX_TRACKED_PROFILES` accordingly.
  Profiles nobody opens for `INACTIVE_AFTER_DAYS` stop being snapshotted.

## Project layout

```
run.py              start server + background workers
app/config.py       settings (.env)
app/steam.py        Steam Web API + store API client
app/tracker.py      snapshots, timeline maths, workers
app/history.py      backfill: Steam Replay, Steam log import, gap estimates
app/purchases.py    purchase-history import (HTML parser, game matching, cart splitting)
app/auth.py         Sign in through Steam (OpenID 2.0)
app/web.py          JSON API + page routes
static/             single-page frontend (no build step)
scripts/seed_demo.py, tests/
```

## Known limits

- Steam updates a game's total when a session ends, so play lands on the day the session finished.
- The purchase-history parser expects the English Steam UI (dates like "12 Jun, 2025").
- Multi-game carts are split between items by store price; bundles that don't match an
  owned game are listed separately but still count in the total.

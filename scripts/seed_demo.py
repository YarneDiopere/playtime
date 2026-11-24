"""Create a synthetic demo profile so the dashboard can be tried without a Steam API key.

    python scripts/seed_demo.py     -> then open http://localhost:8000/u/76561190000000001

The data is made up. Store prices for these (real) app IDs are fetched by the price worker.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, purchases  # noqa: E402
from app.tracker import DEMO_STEAMID  # noqa: E402

GAMES = {
    1245620: ("ELDEN RING", 4200),
    1086940: ("Baldur's Gate 3", 2600),
    2379780: ("Balatro", 300),
    730: ("Counter-Strike 2", 9800),
    1145360: ("Hades", 1900),
    413150: ("Stardew Valley", 5200),
    367520: ("Hollow Knight", 1500),
    292030: ("The Witcher 3: Wild Hunt", 6100),
    105600: ("Terraria", 3300),
    646570: ("Slay the Spire", 2400),
    620: ("Portal 2", 700),
    1091500: ("Cyberpunk 2077", 0),
}
# Rough "phases": which games get played in which part of the 150 days.
PHASES = [
    (0, 50, {1245620: 6, 730: 3, 105600: 1}),
    (40, 100, {1086940: 7, 730: 2, 413150: 2, 646570: 1}),
    (95, 150, {2379780: 6, 730: 3, 1145360: 2, 1091500: 2}),
]
DAYS = 150

HISTORY = [
    ("12 Jun, 2025", ["ELDEN RING", "ELDEN RING Shadow of the Erdtree"], "Purchase", "$79.98"),
    ("2 Aug, 2025", ["Baldur's Gate 3"], "Purchase", "$59.99"),
    ("20 Nov, 2025", ["Hades", "Hollow Knight"], "Purchase", "$19.98"),
    ("21 Nov, 2025", ["Hollow Knight"], "Refund", "$7.49"),
    ("3 Jan, 2026", ["Counter-Strike 2", "Operation Pass"], "In-Game Purchase", "$14.99"),
    ("14 Feb, 2026", ["Steam Wallet"], "Wallet Credit", "$20.00"),
    ("6 Apr, 2026", ["Stardew Valley", "Slay the Spire", "Terraria"], "Purchase", "$23.97"),
    ("1 Jun, 2026", ["Balatro"], "Purchase", "$14.99"),
    ("8 Aug, 2026", ["Cyberpunk 2077", "Cyberpunk 2077: Phantom Liberty"], "Purchase", "$47.99"),
    ("2 Sep, 2026", ["Valve Complete Pack"], "Purchase", "$29.99"),
]


def history_html() -> str:
    rows = []
    for date, items, kind, total in HISTORY:
        item_divs = "".join(f"<div>{i}</div>" for i in items)
        rows.append(
            f'<tr class="wallet_table_row"><td class="wht_date">{date}</td>'
            f'<td class="wht_items">{item_divs}<div class="wth_payment">Visa</div></td>'
            f'<td class="wht_type"><div>{kind}</div><div class="wth_payment">Visa</div></td>'
            f'<td class="wht_total">{total}</td><td class="wht_wallet_change"></td></tr>'
        )
    return f'<html><body><table class="wallet_history_table">{"".join(rows)}</table></body></html>'


def main() -> None:
    random.seed(7)
    db.init()
    c = db.conn()
    for table in ("snapshots", "user_games", "purchase_items", "purchases", "users"):
        c.execute(f"DELETE FROM {table} WHERE steamid=?", (DEMO_STEAMID,))

    now = db.now()
    start = now - DAYS * 86400
    c.execute(
        "INSERT INTO users (steamid, persona, avatar, profile_url, added_at, last_viewed_at, last_snapshot_at, share_spending) "
        "VALUES (?,?,?,?,?,?,?,1)",
        (DEMO_STEAMID, "Demo Player", None, None, start, now, now - 600),
    )
    totals = {}
    for appid, (name, before) in GAMES.items():
        c.execute("INSERT INTO games (appid, name) VALUES (?,?) ON CONFLICT(appid) DO NOTHING", (appid, name))
        totals[appid] = before
        if before:
            c.execute(
                "INSERT INTO snapshots (steamid, appid, ts, playtime_forever, baseline) VALUES (?,?,?,?,1)",
                (DEMO_STEAMID, appid, start, before),
            )

    last_played = {}
    recent = {a: 0 for a in GAMES}
    for day in range(DAYS):
        weekend = (day % 7) in (5, 6)
        if random.random() < (0.15 if not weekend else 0.05):
            continue  # day off
        weights = {}
        for lo, hi, w in PHASES:
            if lo <= day < hi:
                for a, x in w.items():
                    weights[a] = weights.get(a, 0) + x
        wildcard = random.choice(list(GAMES))
        weights[wildcard] = weights.get(wildcard, 0) + 0.5
        for _ in range(random.choice([1, 1, 2, 2, 3])):
            appid = random.choices(list(weights), list(weights.values()))[0]
            minutes = int(random.gauss(110 if weekend else 70, 40))
            if minutes < 10:
                continue
            ts = start + day * 86400 + random.randint(17, 23) * 3600
            totals[appid] += minutes
            last_played[appid] = ts
            if day >= DAYS - 14:
                recent[appid] += minutes
            c.execute(
                "INSERT INTO snapshots (steamid, appid, ts, playtime_forever) VALUES (?,?,?,?)",
                (DEMO_STEAMID, appid, ts, totals[appid]),
            )
    for appid in GAMES:
        c.execute(
            "INSERT INTO user_games (steamid, appid, playtime_forever, playtime_2weeks, rtime_last_played, first_seen) "
            "VALUES (?,?,?,?,?,?)",
            (DEMO_STEAMID, appid, totals[appid], recent[appid], last_played.get(appid), start),
        )
    c.commit()
    result = purchases.import_history(DEMO_STEAMID, history_html(), "us")
    print(f"Demo profile ready: /u/{DEMO_STEAMID}  (purchase import: {result})")


if __name__ == "__main__":
    main()

"""Run with: python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DATABASE_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date  # noqa: E402

from app import db, history, purchases, steam, tracker  # noqa: E402

SID = str(76561197960265728 + 42)  # account id 42


class ParseTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(purchases.parse_amount("$19.99"), (1999, "$"))
        self.assertEqual(purchases.parse_amount("19,99€"), (1999, "€"))
        self.assertEqual(purchases.parse_amount("$1,234.50"), (123450, "$"))
        self.assertEqual(purchases.parse_amount("1.234,50 €"), (123450, "€"))
        self.assertEqual(purchases.parse_amount("1 299 ₽"), (129900, "₽"))
        self.assertIsNone(purchases.parse_amount("Free"))

    def test_currency_codes(self):
        from app import fx
        self.assertEqual(fx.currency_code("$USD"), "USD")
        self.assertEqual(fx.currency_code("₴"), "UAH")
        self.assertEqual(fx.currency_code("€"), "EUR")
        self.assertEqual(fx.currency_code("CDN$"), "CAD")
        self.assertEqual(fx.currency_code("zł"), "PLN")
        rates = {"EUR": 1.0, "UAH": 50.0}
        self.assertEqual(fx.to_eur(335200, "UAH", rates, {}), 6704)  # ₴3,352 -> €67.04
        self.assertEqual(fx.from_eur(6704, "UAH", rates), 335200)

    def test_dates(self):
        self.assertIsNotNone(purchases.parse_date("12 Jun, 2025"))
        self.assertIsNotNone(purchases.parse_date("Jun 12, 2025"))
        self.assertIsNone(purchases.parse_date("12 juin 2025"))

    def test_resolve_direct_ids(self):
        self.assertEqual(steam.resolve_steamid(SID), SID)
        self.assertEqual(steam.resolve_steamid(f"https://steamcommunity.com/profiles/{SID}/"), SID)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("snapshots", "user_games", "users", "purchase_items", "purchases"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
        self._orig = (steam.player_summary, steam.owned_games, steam.year_in_review)
        steam.player_summary = lambda sid: {"personaname": "Tester"}
        steam.year_in_review = lambda sid, year: None  # no network in tests
        from app import fx
        self._orig_fx = fx.rates_for
        fx.rates_for = lambda days: {d: {"EUR": 1.0, "USD": 1.0} for d in days}

    def tearDown(self):
        from app import fx
        steam.player_summary, steam.owned_games, steam.year_in_review = self._orig
        fx.rates_for = self._orig_fx

    def snap(self, games, ts_offset=0):
        steam.owned_games = lambda sid: [{"appid": a, "name": f"Game {a}", "playtime_forever": p} for a, p in games.items()]
        tracker.snapshot_user(SID)

    def test_timeline_counts_only_new_play(self):
        tracker.ensure_user(SID)
        self.snap({10: 600, 20: 0})          # baseline: 10h of history not on the timeline
        self.snap({10: 600, 20: 0})          # no change -> no rows
        self.snap({10: 690, 20: 30, 30: 15})  # +90, +30, and a newly bought game with 15
        tl = tracker.timeline(SID, 7, 0)
        today = tl["days"][-1]
        self.assertEqual(today["games"], {10: 90, 20: 30, 30: 15})
        self.assertEqual(today["total"], 135)
        rows = db.conn().execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertEqual(rows, 4)  # 1 baseline + 3 changes

    def test_private_profile_records_error(self):
        tracker.ensure_user(SID)

        def private(sid):
            raise steam.PrivateProfile("private")
        steam.owned_games = private
        with self.assertRaises(steam.PrivateProfile):
            tracker.snapshot_user(SID)
        err = db.conn().execute("SELECT last_error FROM users WHERE steamid=?", (SID,)).fetchone()[0]
        self.assertEqual(err, "private")

    def test_purchase_import_matching_and_refunds(self):
        tracker.ensure_user(SID)
        steam.owned_games = lambda sid: [
            {"appid": 1, "name": "Hades™", "playtime_forever": 60},
            {"appid": 2, "name": "Hollow Knight", "playtime_forever": 0},
        ]
        tracker.snapshot_user(SID)
        html = """<table>
          <tr class="wallet_table_row"><td class="wht_date">20 Nov, 2025</td>
            <td class="wht_items"><div>Hades</div><div>Hollow Knight</div><div class="wth_payment">Visa</div></td>
            <td class="wht_type"><div>Purchase</div></td><td class="wht_total">$20.00</td></tr>
          <tr class="wallet_table_row"><td class="wht_date">21 Nov, 2025</td>
            <td class="wht_items"><div>Hollow Knight</div></td>
            <td class="wht_type"><div>Refund</div></td><td class="wht_total">$10.00</td></tr>
          <tr class="wallet_table_row"><td class="wht_date">22 Nov, 2025</td>
            <td class="wht_items"><div>Hades - Original Soundtrack</div></td>
            <td class="wht_type"><div>Purchase</div></td><td class="wht_total">$5.00</td></tr>
          <tr class="wallet_table_row"><td class="wht_date">23 Nov, 2025</td>
            <td class="wht_items"><div>Steam Wallet</div></td>
            <td class="wht_type"><div>Wallet Credit</div></td><td class="wht_total">$50.00</td></tr>
        </table>"""
        result = purchases.import_history(SID, html, "us")
        self.assertEqual(result["transactions"], 3)  # wallet credit skipped
        paid = dict(db.conn().execute(
            "SELECT appid, SUM(amount) FROM purchase_items WHERE steamid=? GROUP BY appid", (SID,)
        ).fetchall())
        self.assertEqual(paid, {1: 1500, 2: 0})  # Hades 10 + soundtrack 5; Knight 10 - 10 refunded


LOG = """[2026-05-06 19:53:56] Client version: 1778003620
[2026-05-06 21:40:11] AppID 10 adding PID 1 as a tracked process "game.exe"
[2026-05-06 21:40:20] AppID 10 adding PID 2 as a tracked process "launcher.exe"
[2026-05-06 22:10:11] AppID 10 no longer tracking PID 1, exit code 0
[2026-05-06 22:40:11] AppID 10 no longer tracking PID 2, exit code 0
[2026-05-07 23:30:00] AppID 20 adding PID 3 as a tracked process "other.exe"
[2026-05-08 00:30:00] AppID 20 no longer tracking PID 3, exit code 0
[2026-05-08 10:00:00] AppID 30 adding PID 4 as a tracked process "x.exe"
[2026-05-08 11:00:00] AppID 30 no longer tracking PID 4, exit code 0
"""
# Account 42 (= SID) signed in until 2026-05-08 09:00, then another account.
CONNECTION_LOG = """[2026-05-06 19:53:11] [Logged Off, 0, 0] [U:1:0] CCMInterface::SetSteamID( [U:1:0] )
[2026-05-06 19:54:00] [Logged On, 0, 0] [U:1:42] connected
[2026-05-08 09:00:00] [Logged On, 0, 0] [U:1:99] connected
"""


class LogTests(unittest.TestCase):
    def setUp(self):
        SnapshotTests.setUp(self)
        db.conn().execute("DELETE FROM log_sessions")
        db.conn().execute("DELETE FROM log_coverage")

    tearDown = SnapshotTests.tearDown
    snap = SnapshotTests.snap

    def test_parse_sessions(self):
        sessions, span = history.parse_gameprocess_log(LOG, 0)
        by_app = {a: (e - s) // 60 for a, s, e in sessions}
        self.assertEqual(by_app, {10: 60, 20: 60, 30: 60})  # overlapping PIDs merge into one session
        self.assertIsNotNone(span)

    def test_import_filters_other_accounts_and_splits_midnight(self):
        tracker.ensure_user(SID)
        self.snap({10: 600, 20: 600, 30: 600})
        r = history.import_logs(SID, [LOG, CONNECTION_LOG], 0)
        self.assertEqual((r["sessions_added"], r["sessions_other_account"]), (2, 1))
        per_day, meta = history.backfill(SID, date(2026, 6, 1), 0)
        self.assertAlmostEqual(per_day[date(2026, 5, 6)][10]["log"], 60)
        self.assertAlmostEqual(per_day[date(2026, 5, 7)][20]["log"], 30)  # 23:30-00:00
        self.assertAlmostEqual(per_day[date(2026, 5, 8)][20]["log"], 30)  # 00:00-00:30
        self.assertNotIn(30, per_day[date(2026, 5, 8)])  # played by the other account
        self.assertEqual(meta["log_range"], ["2026-05-06", "2026-05-08"])


if __name__ == "__main__":
    unittest.main()

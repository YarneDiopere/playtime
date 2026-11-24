"""Copy the local database for the published accounts only, without any purchase data.

    python scripts/export_seed_db.py 7656119... 7656119... -> data/seed.db

Used once to give the GitHub Action the history already collected locally
(baseline snapshots, Steam log sessions, Steam Replay).
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402

PER_USER = ["users", "user_games", "snapshots", "replay_fetches", "replay_months", "replay_games",
            "log_sessions", "log_coverage"]
DROP = ["purchases", "purchase_items", "fx_rates"]


def main(accounts: list[str]) -> None:
    out = ROOT / "data" / "seed.db"
    out.unlink(missing_ok=True)
    src = sqlite3.connect(config.DATABASE_PATH)
    dst = sqlite3.connect(out)
    src.backup(dst)
    src.close()
    q = ",".join("?" * len(accounts))
    for t in PER_USER:
        dst.execute(f"DELETE FROM {t} WHERE steamid NOT IN ({q})", accounts)
    for t in DROP:
        dst.execute(f"DELETE FROM {t}")
    dst.execute("UPDATE users SET share_spending=0")
    dst.commit()
    dst.execute("VACUUM")
    for t in PER_USER + DROP:
        print(t, dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    dst.close()
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])

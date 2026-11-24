"""Start the tracker: python run.py"""
from waitress import serve

from app import config, db, tracker
from app.web import app

if __name__ == "__main__":
    db.init()
    if not config.STEAM_API_KEY:
        print("[warn] STEAM_API_KEY is not set - profile lookups will fail. See README.md.")
    if config.RUN_WORKERS:
        tracker.start_workers()
    print(f"Steam playtime tracker running on http://{config.HOST}:{config.PORT}")
    serve(app, host=config.HOST, port=config.PORT, threads=8)

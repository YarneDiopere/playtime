"""Refresh the live page shortly after you close a Steam game.

Watches Steam's logs/gameprocess_log.txt. When a game's last process exits, it waits a
moment (Steam needs to report the session to its servers) and then starts the GitHub
workflow that snapshots playtime and republishes yarnediopere.dev/playtime.

Runs hidden at Windows sign-in via a shortcut in the Startup folder (see README).
Uses the GitHub CLI login that's already on this PC. Log: data/watcher.log
"""
import os
import re
import shutil
import subprocess
import sys
import time
import winreg
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = os.environ.get("PLAYTIME_REPO", "YarneDiopere/playtime")
WORKFLOW = "update-site.yml"
SETTLE_SECONDS = 90        # let Steam upload the session before snapshotting
MIN_GAP_SECONDS = 5 * 60   # never trigger more than once per 5 minutes
POLL_SECONDS = 5

ADD_RE = re.compile(r"AppID (\d+) adding PID (\d+) as a tracked process")
REMOVE_RE = re.compile(r"AppID (\d+) no longer tracking PID (\d+)")
LOG_FILE = ROOT / "data" / "watcher.log"


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if sys.stdout:
        print(line, flush=True)


def steam_log_path() -> Path:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        steam = r"C:\Program Files (x86)\Steam"
    return Path(steam) / "logs" / "gameprocess_log.txt"


def gh_exe() -> str:
    return shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"


def trigger(reason: str) -> bool:
    try:
        r = subprocess.run(
            [gh_exe(), "workflow", "run", WORKFLOW, "-R", REPO, "--ref", "main"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"could not start the update ({reason}): {e}")
        return False
    if r.returncode != 0:
        log(f"update failed ({reason}): {(r.stderr or r.stdout).strip()}")
        return False
    log(f"page update started ({reason})")
    return True


def main() -> None:
    path = steam_log_path()
    log(f"watching {path}")
    live: dict[str, set] = {}
    pos = path.stat().st_size if path.exists() else 0  # only react to new lines
    pending_since = None
    pending_apps: set[str] = set()
    last_trigger = 0.0

    while True:
        try:
            size = path.stat().st_size if path.exists() else 0
            if size < pos:  # Steam rotated the log
                pos = 0
            if size > pos:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    if m := ADD_RE.search(line):
                        live.setdefault(m.group(1), set()).add(m.group(2))
                    elif m := REMOVE_RE.search(line):
                        app, pid = m.group(1), m.group(2)
                        live.get(app, set()).discard(pid)
                        if not live.get(app):
                            live.pop(app, None)
                            pending_apps.add(app)
                            pending_since = time.time()

            now = time.time()
            if pending_since and now - pending_since >= SETTLE_SECONDS and now - last_trigger >= MIN_GAP_SECONDS:
                if trigger(f"closed app {', '.join(sorted(pending_apps))}"):
                    last_trigger = now
                pending_since, pending_apps = None, set()
        except Exception as e:  # keep watching whatever happens
            log(f"error: {e!r}")
            time.sleep(30)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if "--test" in sys.argv:
        trigger("manual test")
    else:
        main()

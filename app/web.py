"""HTTP routes: JSON API + the single-page frontend."""
import os
import threading
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, redirect, request, send_from_directory, session

from . import auth, config, db, fx, history, purchases, steam, tracker

# Currency shown for spending, matching the store region picked for prices.
REGION_CURRENCY = {"us": "USD", "de": "EUR", "fr": "EUR", "nl": "EUR", "gb": "GBP", "ua": "UAH", "pl": "PLN",
                   "ca": "CAD", "au": "AUD", "br": "BRL", "jp": "JPY", "tr": "TRY", "ru": "RUB"}

STATIC = config.ROOT / "static"

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.secret_key = config.SECRET_KEY
app.config.update(
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,  # saved history pages can be large
    SEND_FILE_MAX_AGE_DEFAULT=0,  # browsers revalidate static files, so updates show up immediately
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.BASE_URL.startswith("https://"),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)
if os.environ.get("TRUST_PROXY") == "1":  # behind nginx/Caddy/etc.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

_new_profile_hits: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()
MIN_MANUAL_REFRESH_SECONDS = 300


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if config.BASE_URL.startswith("https://"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


def error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def viewer() -> str | None:
    return session.get("steamid")


def require_ajax():
    # Extra CSRF guard on top of SameSite cookies: browsers won't send this header cross-site.
    return request.headers.get("X-Requested-With") == "fetch"


def allow_new_profile(ip: str) -> bool:
    with _rate_lock:
        hits = _new_profile_hits[ip]
        cutoff = time.time() - 3600
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= config.NEW_PROFILES_PER_IP_PER_HOUR:
            return False
        hits.append(time.time())
        return True


# ---------------------------------------------------------------- pages

@app.get("/")
@app.get("/u/<path:_steamid>")
def index(_steamid=None):
    return send_from_directory(STATIC, "index.html")


# ---------------------------------------------------------------- auth

@app.get("/auth/login")
def login():
    return redirect(auth.login_url())


@app.get("/auth/callback")
def callback():
    steamid = auth.verify(request.args.to_dict())
    if not steamid:
        return redirect("/?login=failed")
    session.permanent = True
    session["steamid"] = steamid
    if tracker.ensure_user(steamid):
        try:
            tracker.snapshot_user(steamid)
        except steam.SteamError:
            pass
    return redirect(f"/u/{steamid}")


@app.post("/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- api

@app.get("/api/config")
def api_config():
    me = viewer()
    persona = None
    if me:
        row = db.conn().execute("SELECT persona, avatar FROM users WHERE steamid=?", (me,)).fetchone()
        persona = dict(row) if row else None
    return jsonify({
        "regions": config.PRICE_REGIONS,
        "default_region": config.DEFAULT_REGION,
        "snapshot_interval_minutes": config.SNAPSHOT_INTERVAL_MINUTES,
        "me": {"steamid": me, **(persona or {})} if me else None,
    })


@app.get("/api/resolve")
def api_resolve():
    try:
        return jsonify({"steamid": steam.resolve_steamid(request.args.get("q", ""))})
    except steam.SteamError as e:
        return error(str(e), 404)


def _check_steamid(steamid: str):
    return steam.STEAMID_RE.match(steamid) is not None


@app.get("/api/profile/<steamid>")
def api_profile(steamid):
    if not _check_steamid(steamid):
        return error("Invalid SteamID", 404)
    c = db.conn()
    region = request.args.get("region", config.DEFAULT_REGION).lower()
    if region not in config.PRICE_REGIONS:
        region = config.DEFAULT_REGION

    tracked = c.execute("SELECT 1 FROM users WHERE steamid=?", (steamid,)).fetchone()
    if not tracked:
        count = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count >= config.MAX_TRACKED_PROFILES:
            return error("This site is tracking as many profiles as it can right now.", 503)
        if viewer() != steamid and not allow_new_profile(request.remote_addr or "?"):
            return error("Too many new profiles from your network. Try again in an hour.", 429)
    newly_added = tracker.ensure_user(steamid)
    if newly_added:
        try:
            tracker.snapshot_user(steamid)
        except steam.SteamError:
            pass  # stored as last_error and shown on the page
    tracker.request_region(region)

    user = dict(c.execute("SELECT * FROM users WHERE steamid=?", (steamid,)).fetchone())
    is_owner = viewer() == steamid
    can_see_spending = is_owner or bool(user["share_spending"])

    paid = {}
    show_cur = REGION_CURRENCY.get(region, "EUR")
    latest = {}
    has_purchases = can_see_spending and c.execute("SELECT 1 FROM purchases WHERE steamid=? LIMIT 1", (steamid,)).fetchone()
    if has_purchases:
        fx.convert_purchases(steamid)  # no-op once every purchase has its EUR amount
        latest = fx.rates_for(["latest"])["latest"]
        paid = {
            r["appid"]: fx.from_eur(r["amount"], show_cur, latest)
            for r in c.execute(
                "SELECT appid, SUM(amount_eur) AS amount FROM purchase_items "
                "WHERE steamid=? AND appid IS NOT NULL GROUP BY appid",
                (steamid,),
            )
        }

    games = []
    for r in c.execute(
        "SELECT ug.appid, g.name, g.img_icon, g.capsule, ug.playtime_forever, ug.playtime_2weeks, ug.rtime_last_played, "
        "ug.first_seen, p.currency, p.initial, p.final, p.is_free, p.available, p.fetched_at "
        "FROM user_games ug JOIN games g ON g.appid = ug.appid "
        "LEFT JOIN prices p ON p.appid = ug.appid AND p.region = ? "
        "WHERE ug.steamid = ? ORDER BY ug.playtime_forever DESC",
        (region, steamid),
    ):
        g = dict(r)
        g["price_known"] = g.pop("fetched_at") is not None
        g["paid"] = paid.get(g["appid"])
        games.append(g)

    priced = [g for g in games if g["price_known"] and g["available"] and not g["is_free"] and g["initial"]]
    currency = next((g["currency"] for g in priced if g["currency"]), None)
    stats = {
        "games_owned": len(games),
        "games_played": sum(1 for g in games if g["playtime_forever"] > 0),
        "total_minutes": sum(g["playtime_forever"] for g in games),
        "minutes_2weeks": sum(g["playtime_2weeks"] for g in games),
        "value_regular": sum(g["initial"] for g in priced),
        "value_current": sum(g["final"] or 0 for g in priced),
        "value_currency": currency,
        "prices_pending": sum(1 for g in games if not g["price_known"]),
        "not_for_sale": sum(1 for g in games if g["price_known"] and not g["available"]),
    }

    spending = None
    if can_see_spending:
        conv = lambda v: fx.from_eur(v, show_cur, latest)  # noqa: E731
        spending = {"imported": False, "totals": [], "unmatched": [], "monthly": [], "originals": [], "unconverted": 0}
        if has_purchases:
            t = c.execute(
                "SELECT SUM(total_eur) AS net, SUM(CASE WHEN total_eur > 0 THEN total_eur ELSE 0 END) AS gross, "
                "SUM(CASE WHEN total_eur < 0 THEN -total_eur ELSE 0 END) AS refunded, COUNT(*) AS n, "
                "SUM(total_eur IS NULL) AS unconverted FROM purchases WHERE steamid=?",
                (steamid,),
            ).fetchone()
            originals = c.execute(
                "SELECT COALESCE(code, currency) AS currency, SUM(total) AS net, COUNT(*) AS n FROM purchases "
                "WHERE steamid=? GROUP BY COALESCE(code, currency) ORDER BY n DESC",
                (steamid,),
            ).fetchall()
            unmatched = c.execute(
                "SELECT name, SUM(amount_eur) AS amount FROM purchase_items WHERE steamid=? AND appid IS NULL "
                "GROUP BY name ORDER BY amount DESC LIMIT 50",
                (steamid,),
            ).fetchall()
            monthly = c.execute(
                "SELECT strftime('%Y-%m', ts, 'unixepoch') AS month, SUM(total_eur) AS net FROM purchases "
                "WHERE steamid=? AND ts IS NOT NULL GROUP BY month ORDER BY month",
                (steamid,),
            ).fetchall()
            spending = {
                "imported": True,
                "totals": [{
                    "currency": show_cur, "net": conv(t["net"] or 0), "gross": conv(t["gross"] or 0),
                    "refunded": conv(t["refunded"] or 0), "n": t["n"],
                }],
                "originals": [dict(o) for o in originals],
                "unconverted": t["unconverted"] or 0,
                "unmatched": [{"name": u["name"], "amount": conv(u["amount"])} for u in unmatched],
                "monthly": [{"month": m["month"], "net": conv(m["net"] or 0)} for m in monthly],
            }

    return jsonify({
        "steamid": steamid,
        "persona": user["persona"],
        "avatar": user["avatar"],
        "profile_url": user["profile_url"],
        "tracking_since": user["added_at"],
        "last_snapshot_at": user["last_snapshot_at"],
        "last_error": user["last_error"],
        "share_spending": bool(user["share_spending"]),
        "is_owner": is_owner,
        "can_see_spending": can_see_spending,
        "region": region,
        "stats": stats,
        "games": games,
        "spending": spending,
    })


@app.get("/api/profile/<steamid>/timeline")
def api_timeline(steamid):
    if not _check_steamid(steamid):
        return error("Invalid SteamID", 404)
    days = max(1, min(int(request.args.get("days", 90)), 3650))
    tz = max(-840, min(int(request.args.get("tz", 0)), 840))
    return jsonify(tracker.timeline(steamid, days, tz))


@app.post("/api/profile/<steamid>/refresh")
def api_refresh(steamid):
    if not require_ajax():
        return error("Bad request")
    row = db.conn().execute("SELECT last_snapshot_at, last_error FROM users WHERE steamid=?", (steamid,)).fetchone()
    if row is None:
        return error("Profile isn't tracked", 404)
    # A failed snapshot (e.g. profile was private) can be retried right away.
    if not row["last_error"] and row["last_snapshot_at"] and db.now() - row["last_snapshot_at"] < MIN_MANUAL_REFRESH_SECONDS:
        return error("Refreshed less than 5 minutes ago.", 429)
    try:
        tracker.snapshot_user(steamid)
    except steam.SteamError as e:
        return error(str(e), 502)
    return jsonify({"ok": True})


@app.post("/api/me/purchases")
def api_import():
    me = viewer()
    if not me:
        return error("Sign in through Steam first.", 401)
    if not require_ajax():
        return error("Bad request")
    upload = request.files.get("file")
    html = upload.read().decode("utf-8", "replace") if upload else request.form.get("html", "")
    if not html.strip():
        return error("Upload the saved purchase history page.")
    region = request.form.get("region", config.DEFAULT_REGION)
    try:
        return jsonify(purchases.import_history(me, html, region))
    except purchases.ImportError_ as e:
        return error(str(e))


@app.delete("/api/me/purchases")
def api_delete_import():
    me = viewer()
    if not me or not require_ajax():
        return error("Sign in through Steam first.", 401)
    purchases.delete_history(me)
    return jsonify({"ok": True})


@app.post("/api/me/playlog")
def api_import_log():
    me = viewer()
    if not me:
        return error("Sign in through Steam first.", 401)
    if not require_ajax():
        return error("Bad request")
    texts = [f.read().decode("utf-8", "replace") for f in request.files.getlist("files")]
    if not texts:
        return error("Choose gameprocess_log.txt (and gameprocess_log.previous.txt if you have it).")
    tz = max(-840, min(int(request.form.get("tz", 0)), 840))
    try:
        return jsonify(history.import_logs(me, texts, tz))
    except history.LogImportError as e:
        return error(str(e))


@app.delete("/api/me/playlog")
def api_delete_log():
    me = viewer()
    if not me or not require_ajax():
        return error("Sign in through Steam first.", 401)
    history.delete_logs(me)
    return jsonify({"ok": True})


@app.post("/api/me/settings")
def api_settings():
    me = viewer()
    if not me or not require_ajax():
        return error("Sign in through Steam first.", 401)
    body = request.get_json(silent=True) or {}
    if "share_spending" in body:
        c = db.conn()
        c.execute("UPDATE users SET share_spending=? WHERE steamid=?", (1 if body["share_spending"] else 0, me))
        c.commit()
    return jsonify({"ok": True})

"""Currency conversion for imported purchases.

Steam accounts can hold purchases in several currencies (e.g. after a region change).
Each purchase is converted to EUR at the rate of its purchase date and stored; for display
the EUR amount is converted to the viewer's chosen currency at today's rate.

Rate sources (free, no key):
  fawazahmed0/currency-api via jsdelivr - daily, ~200 currencies incl. UAH, from March 2024
  Frankfurter (ECB reference rates)     - daily since 1999, ~30 major currencies
"""
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import db

FAWAZ = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{day}/v1/currencies/eur.json"
FRANKFURTER = "https://api.frankfurter.app/{day}?from=EUR"

SYMBOLS = {
    "€": "EUR", "$": "USD", "US$": "USD", "£": "GBP", "₴": "UAH", "₽": "RUB", "pуб": "RUB", "руб": "RUB",
    "zł": "PLN", "R$": "BRL", "CDN$": "CAD", "C$": "CAD", "A$": "AUD", "NZ$": "NZD", "₩": "KRW", "₹": "INR",
    "₺": "TRY", "TL": "TRY", "CHF": "CHF", "kr": "SEK", "Kč": "CZK", "₸": "KZT", "₪": "ILS", "HK$": "HKD",
    "S$": "SGD", "Mex$": "MXN", "P": "PHP", "฿": "THB", "Rp": "IDR", "RM": "MYR", "₫": "VND", "R": "ZAR",
    "CLP$": "CLP", "COL$": "COP", "S/.": "PEN", "₡": "CRC", "¥": "JPY", "NT$": "TWD", "SR": "SAR", "AED": "AED",
    "QR": "QAR", "KD": "KWD", "ARS$": "ARS", "$U": "UYU", "₼": "AZN", "лв": "BGN", "Ft": "HUF", "lei": "RON",
}


def currency_code(text: str | None) -> str | None:
    """'$USD' -> USD, '€' -> EUR, '₴' -> UAH. None when unknown."""
    if not text:
        return None
    m = re.search(r"\b([A-Z]{3})\b|([A-Z]{3})$", text)
    if m and (m.group(1) or m.group(2)) not in ("CDN",):
        return m.group(1) or m.group(2)
    t = text.strip()
    for sym in sorted(SYMBOLS, key=len, reverse=True):
        if sym in t:
            return SYMBOLS[sym]
    return None


def _fetch(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "steam-playtime-tracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def _download(day: str) -> dict[str, float]:
    """Units of each currency per 1 EUR on that day (latest if day == 'latest')."""
    rates: dict[str, float] = {}
    ecb = _fetch(FRANKFURTER.format(day=day))
    if ecb and "rates" in ecb:
        rates.update({k.upper(): float(v) for k, v in ecb["rates"].items()})
    fz = _fetch(FAWAZ.format(day=day))
    if fz and "eur" in fz:
        for k, v in fz["eur"].items():
            if v and len(k) == 3:
                rates.setdefault(k.upper(), float(v))
    rates["EUR"] = 1.0
    return rates


def _ensure_table() -> None:
    db.conn().execute("CREATE TABLE IF NOT EXISTS fx_rates (day TEXT PRIMARY KEY, rates TEXT NOT NULL, fetched_at INTEGER NOT NULL)")


def rates_for(days: list[str]) -> dict[str, dict[str, float]]:
    """Rates per day, cached in the database. 'latest' is refreshed every 12 hours."""
    _ensure_table()
    c = db.conn()
    out, missing = {}, []
    for day in set(days):
        row = c.execute("SELECT rates, fetched_at FROM fx_rates WHERE day=?", (day,)).fetchone()
        if row and (day != "latest" or db.now() - row["fetched_at"] < 12 * 3600):
            out[day] = json.loads(row["rates"])
        else:
            missing.append(day)
    if missing:
        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = dict(zip(missing, pool.map(_download, missing)))
        for day, rates in fetched.items():
            if len(rates) > 1:  # only cache real answers
                c.execute("INSERT OR REPLACE INTO fx_rates (day, rates, fetched_at) VALUES (?,?,?)",
                          (day, json.dumps(rates), db.now()))
            out[day] = rates
        c.commit()
    return out


def to_eur(amount: int, code: str | None, rates: dict[str, float], fallback: dict[str, float]) -> int | None:
    if code is None:
        return None
    r = rates.get(code) or fallback.get(code)
    return round(amount / r) if r else None


def from_eur(amount_eur: int | None, code: str, latest: dict[str, float]) -> int | None:
    if amount_eur is None:
        return None
    r = latest.get(code)
    return round(amount_eur * r) if r else None


def convert_purchases(steamid: str) -> int:
    """Fill in EUR amounts for purchases that don't have them yet. Returns rows converted."""
    c = db.conn()
    rows = c.execute(
        "SELECT id, ts, total, currency FROM purchases WHERE steamid=? AND total_eur IS NULL", (steamid,)
    ).fetchall()
    if not rows:
        return 0
    day_of = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "latest"  # noqa: E731
    rates = rates_for([day_of(r["ts"]) for r in rows] + ["latest"])
    latest = rates["latest"]
    for r in rows:
        code = currency_code(r["currency"])
        eur = to_eur(r["total"], code, rates.get(day_of(r["ts"]), {}), latest)
        c.execute("UPDATE purchases SET code=?, total_eur=? WHERE id=?", (code, eur, r["id"]))
        # Items keep their share of the purchase, now in EUR.
        items = c.execute("SELECT rowid, amount FROM purchase_items WHERE purchase_id=?", (r["id"],)).fetchall()
        for it in items:
            share = round(eur * it["amount"] / r["total"]) if (eur is not None and r["total"]) else None
            c.execute("UPDATE purchase_items SET amount_eur=? WHERE rowid=?", (share, it["rowid"]))
    c.commit()
    return len(rows)

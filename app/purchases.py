"""Import a saved copy of https://store.steampowered.com/account/history/ .

Steam has no API for purchase history, so the owner saves that page (after clicking
"Load more" until everything is shown) and uploads the HTML here.
"""
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from . import db

DATE_FORMATS = ["%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%d %b %Y", "%b %d %Y", "%Y-%m-%d", "%d.%m.%Y"]


class ImportError_(Exception):
    pass


def normalize(name: str) -> str:
    name = re.sub(r"[™®©]", "", name.lower())
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def parse_amount(text: str) -> tuple[int, str] | None:
    """'€19,99' -> (1999, '€'), '$1,234.50' -> (123450, '$'), '1 299 ₽' -> (129900, '₽')."""
    m = re.search(r"\d[\d.,\s ']*", text)
    if not m:
        return None
    raw = re.sub(r"[\s ']", "", m.group(0)).rstrip(".,")
    currency = re.sub(r"[\d.,\s '+\-]", "", text).strip()[:6]
    dec = re.search(r"[.,](\d{1,2})$", raw)
    if dec:
        whole = re.sub(r"[.,]", "", raw[: dec.start()])
        cents = int(whole or 0) * 100 + int(dec.group(1).ljust(2, "0"))
    else:
        cents = int(re.sub(r"[.,]", "", raw)) * 100
    return cents, currency


def parse_date(text: str) -> int | None:
    text = re.sub(r"\s+", " ", text).strip()
    for fmt in DATE_FORMATS:
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def _row_type(text: str) -> str | None:
    t = text.lower()
    if "refund" in t:
        return "refund"
    if "in-game" in t or "in game" in t:
        return "ingame"
    if "gift" in t:
        return None  # gifts bought for other people aren't spending on your own games
    if "purchase" in t:
        return "purchase"
    return None  # market transactions, wallet top-ups, conversions...


def _item_lines(cell) -> list[str]:
    lines = []
    for child in cell.find_all(recursive=False):
        classes = " ".join(child.get("class") or [])
        if "wth_payment" in classes:
            continue
        txt = child.get_text(" ", strip=True)
        if txt:
            lines.append(txt)
    if not lines:
        lines = [ln.strip() for ln in cell.get_text("\n").splitlines() if ln.strip()]
    return lines


def parse_history(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr.wallet_table_row")
    if not rows:
        raise ImportError_(
            "No purchase rows found. Save the page from store.steampowered.com/account/history "
            "while signed in (Ctrl+S, 'Webpage, complete' or 'HTML only') and upload that file."
        )
    out = []
    for tr in rows:
        date_td, items_td = tr.select_one("td.wht_date"), tr.select_one("td.wht_items")
        type_td, total_td = tr.select_one("td.wht_type"), tr.select_one("td.wht_total")
        if not (items_td and type_td and total_td):
            continue
        type_first = type_td.find(recursive=False)
        kind = _row_type((type_first or type_td).get_text(" ", strip=True))
        amount = parse_amount(total_td.get_text(" ", strip=True))
        if kind is None or amount is None:
            continue
        date_text = date_td.get_text(" ", strip=True) if date_td else ""
        items = _item_lines(items_td)
        if kind == "ingame":
            items = items[:1]  # the game's name; the following line is the item bought
        out.append({
            "type": kind,
            "date_text": date_text,
            "ts": parse_date(date_text),
            "items": items,
            "total": -amount[0] if kind == "refund" else amount[0],
            "currency": amount[1],
        })
    return out


def _matcher(steamid: str):
    rows = db.conn().execute(
        "SELECT g.appid, g.name FROM user_games ug JOIN games g ON g.appid = ug.appid WHERE ug.steamid=?",
        (steamid,),
    ).fetchall()
    exact = {normalize(r["name"]): r["appid"] for r in rows}
    by_length = sorted(exact.items(), key=lambda kv: -len(kv[0]))

    def match(name: str) -> int | None:
        n = normalize(name)
        if n in exact:
            return exact[n]
        for owned, appid in by_length:  # DLC like "Game Name - Soundtrack" -> Game Name
            if len(owned) >= 4 and n.startswith(owned + " "):
                return appid
        return None

    return match


def import_history(steamid: str, html: str, region: str) -> dict:
    """Replace the owner's imported purchases with the contents of the uploaded page."""
    parsed = parse_history(html)
    match = _matcher(steamid)
    c = db.conn()
    price = {
        r["appid"]: r["initial"]
        for r in c.execute("SELECT appid, initial FROM prices WHERE region=? AND initial IS NOT NULL", (region,))
    }

    c.execute("DELETE FROM purchase_items WHERE steamid=?", (steamid,))
    c.execute("DELETE FROM purchases WHERE steamid=?", (steamid,))
    matched = unmatched = 0
    for p in parsed:
        cur = c.execute(
            "INSERT INTO purchases (steamid, ts, date_text, type, total, currency) VALUES (?,?,?,?,?,?)",
            (steamid, p["ts"], p["date_text"], p["type"], p["total"], p["currency"]),
        )
        items = [(name, match(name)) for name in p["items"]] or [("(unknown item)", None)]
        # Split a multi-item cart by each game's regular price where known, else evenly.
        known = [price[a] for _, a in items if a in price and price[a]]
        fallback = (sum(known) / len(known)) if known else 1
        weights = [price.get(a) or fallback for _, a in items]
        total_w = sum(weights)
        shares = [round(p["total"] * w / total_w) for w in weights]
        shares[-1] += p["total"] - sum(shares)  # rounding remainder
        for (name, appid), share in zip(items, shares):
            c.execute(
                "INSERT INTO purchase_items (purchase_id, steamid, name, appid, amount) VALUES (?,?,?,?,?)",
                (cur.lastrowid, steamid, name, appid, share),
            )
            if appid is None:
                unmatched += 1
            else:
                matched += 1
    c.commit()
    from . import fx  # converts every purchase to EUR at its date's rate
    fx.convert_purchases(steamid)
    return {"transactions": len(parsed), "items_matched": matched, "items_unmatched": unmatched}


def delete_history(steamid: str) -> None:
    c = db.conn()
    c.execute("DELETE FROM purchase_items WHERE steamid=?", (steamid,))
    c.execute("DELETE FROM purchases WHERE steamid=?", (steamid,))
    c.commit()

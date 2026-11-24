"""Thin clients for the Steam Web API (needs a key) and the public store API (no key)."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import config

API = "https://api.steampowered.com"
STORE = "https://store.steampowered.com/api"
USER_AGENT = "steam-playtime-tracker/1.0"


class SteamError(Exception):
    pass


class PrivateProfile(SteamError):
    pass


def _get(url: str, params: dict, timeout: int = 20):
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise SteamError("Steam rate limit hit, try again later") from e
        if e.code in (401, 403):
            raise SteamError("Steam rejected the API key (check STEAM_API_KEY)") from e
        raise SteamError(f"Steam returned HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise SteamError(f"Could not reach Steam: {e}") from e


def _api(path: str, **params):
    if not config.STEAM_API_KEY:
        raise SteamError("STEAM_API_KEY is not set on the server")
    return _get(f"{API}/{path}", {"key": config.STEAM_API_KEY, "format": "json", **params})


STEAMID_RE = re.compile(r"^7656119\d{10}$")


def resolve_steamid(query: str) -> str:
    """Accept a SteamID64, a profile URL (/profiles/ or /id/), or a custom URL name."""
    q = query.strip().rstrip("/")
    if STEAMID_RE.match(q):
        return q
    m = re.search(r"steamcommunity\.com/profiles/(\d+)", q)
    if m and STEAMID_RE.match(m.group(1)):
        return m.group(1)
    m = re.search(r"steamcommunity\.com/id/([^/?#]+)", q)
    vanity = m.group(1) if m else q
    if not re.match(r"^[A-Za-z0-9_-]{2,32}$", vanity):
        raise SteamError("That doesn't look like a SteamID, profile URL or custom URL name")
    data = _api("ISteamUser/ResolveVanityURL/v1/", vanityurl=vanity).get("response", {})
    if data.get("success") != 1:
        raise SteamError(f"No Steam profile found for '{vanity}'")
    return data["steamid"]


def player_summary(steamid: str) -> dict:
    players = _api("ISteamUser/GetPlayerSummaries/v2/", steamids=steamid).get("response", {}).get("players", [])
    if not players:
        raise SteamError("Steam profile not found")
    return players[0]


def owned_games(steamid: str) -> list[dict]:
    data = _api(
        "IPlayerService/GetOwnedGames/v1/",
        steamid=steamid,
        include_appinfo=1,
        include_played_free_games=1,
    ).get("response", {})
    if "games" not in data:
        # Steam returns an empty response when game details are private.
        if data.get("game_count") == 0:
            return []
        raise PrivateProfile(
            "This profile's game details are private. Set 'Game details' to Public in "
            "Steam > Profile > Edit Profile > Privacy Settings."
        )
    return data["games"]


def year_in_review(steamid: str, year: int) -> dict | None:
    """Steam Replay stats for one year, or None when the profile hasn't shared it / no data."""
    data = _api("ISaleFeatureService/GetUserYearInReview/v1/", steamid=steamid, year=year)
    stats = (data.get("response") or {}).get("stats", {}).get("playtime_stats") or {}
    return stats if stats.get("months") else None


ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"


def store_capsules(appids: list[int]) -> dict[int, str]:
    """Small capsule image URL per app (newer games use hashed paths that can't be guessed)."""
    payload = {
        "ids": [{"appid": a} for a in appids],
        "context": {"language": "english", "country_code": "US"},
        "data_request": {"include_assets": True},
    }
    data = _api("IStoreBrowseService/GetItems/v1/", input_json=json.dumps(payload))
    out = {a: "" for a in appids}  # "" = looked up, no image
    for item in (data.get("response") or {}).get("store_items", []):
        assets = item.get("assets") or {}
        fmt, name = assets.get("asset_url_format"), assets.get("small_capsule") or assets.get("header")
        if item.get("appid") and fmt and name:
            out[int(item["appid"])] = ASSET_BASE + fmt.replace("${FILENAME}", name)
    return out


def store_prices(appids: list[int], region: str) -> dict[int, dict]:
    """Batch price lookup (up to ~100 apps per call). Returns appid -> price info."""
    data = _get(
        f"{STORE}/appdetails",
        {"appids": ",".join(map(str, appids)), "cc": region, "filters": "price_overview"},
        timeout=30,
    )
    out = {}
    for key, entry in (data or {}).items():
        appid = int(key)
        if not entry or not entry.get("success"):
            out[appid] = {"available": False}
            continue
        po = entry.get("data") or {}
        po = po.get("price_overview") if isinstance(po, dict) else None
        if not po:
            out[appid] = {"available": True, "is_free": True}  # free or no price listed
        else:
            out[appid] = {
                "available": True,
                "is_free": False,
                "currency": po.get("currency"),
                "initial": po.get("initial"),
                "final": po.get("final"),
            }
    return out

"""Sign in through Steam (OpenID 2.0). Proves which SteamID a visitor owns."""
import re
import urllib.parse
import urllib.request

from . import config

OPENID_URL = "https://steamcommunity.com/openid/login"
NS = "http://specs.openid.net/auth/2.0"
CLAIMED_RE = re.compile(r"^https://steamcommunity\.com/openid/id/(7656119\d{10})$")


def login_url() -> str:
    params = {
        "openid.ns": NS,
        "openid.mode": "checkid_setup",
        "openid.return_to": f"{config.BASE_URL}/auth/callback",
        "openid.realm": config.BASE_URL,
        "openid.identity": f"{NS}/identifier_select",
        "openid.claimed_id": f"{NS}/identifier_select",
    }
    return f"{OPENID_URL}?{urllib.parse.urlencode(params)}"


def verify(args: dict) -> str | None:
    """Ask Steam to confirm the signed response. Returns the SteamID64 or None."""
    if args.get("openid.mode") != "id_res":
        return None
    if not args.get("openid.return_to", "").startswith(f"{config.BASE_URL}/auth/callback"):
        return None
    if args.get("openid.op_endpoint") != OPENID_URL:
        return None
    m = CLAIMED_RE.match(args.get("openid.claimed_id", ""))
    if not m:
        return None
    payload = {k: v for k, v in args.items() if k.startswith("openid.")}
    payload["openid.mode"] = "check_authentication"
    req = urllib.request.Request(
        OPENID_URL,
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
    except OSError:
        return None
    return m.group(1) if "is_valid:true" in body else None

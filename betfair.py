"""Read-only Betfair Exchange client (Betfair Australia login, delayed app key).

What it can do: look up cricket and soccer markets and read back/lay prices.
What it cannot do: place, cancel or change bets. Only the operations in READ_ONLY_OPERATIONS
can be sent; anything else is refused in code before any network call.

Settings (.env):
  BETFAIR_APP_KEY   the "Version 1.0-Delay" key from your Betfair account (free)
  BETFAIR_USERNAME  your Betfair username
  BETFAIR_PASSWORD  your Betfair password (stays in .env on your Mac; never sent anywhere but Betfair)
  BETFAIR_LOGIN_URL default https://identitysso.betfair.com.au/api/login
  BETFAIR_API_URL   default https://api.betfair.com/exchange/betting/json-rpc/v1

Docs: https://betfair-datascientists.github.io/api/apiappkey/
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

READ_ONLY_OPERATIONS = frozenset({
    "listEventTypes", "listCompetitions", "listEvents", "listMarketCatalogue", "listMarketBook",
})
EVENT_TYPES = {"soccer": "1", "football": "1", "cricket": "4"}
SECRET_ENV = ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD")

# Featured soccer competitions, matched against Betfair's competition names (lower case, "contains").
# Override with FEATURED_SOCCER="english premier league,uefa champions league,..." in .env.
FEATURED_SOCCER = (
    "english premier league", "champions league", "europa league", "conference league",
    "german bundesliga", "spanish la liga", "french ligue 1", "italian serie a",
    "fa cup", "efl cup", "carabao cup", "copa del rey", "dfb pokal", "coppa italia", "coupe de france",
    "community shield", "uefa super cup", "club world cup",
    "fifa world cup", "world cup qualifiers", "uefa european championship", "euro 20", "euro qualifiers",
    "nations league", "international friendlies", "copa america", "african cup of nations", "afc asian cup",
)

# A failed login is not retried for this long, so a wrong password can never lock the account.
LOGIN_BACKOFF_S = 30 * 60
# Betfair sessions last about 12 hours; we refresh well before that.
SESSION_MAX_AGE_S = 8 * 60 * 60


def session_file() -> Path:
    return Path(os.environ.get("BETFAIR_SESSION_FILE", Path(__file__).with_name(".betfair_session.json")))


def login_url() -> str:
    return os.environ.get("BETFAIR_LOGIN_URL", "https://identitysso.betfair.com.au/api/login")


def api_url() -> str:
    return os.environ.get("BETFAIR_API_URL", "https://api.betfair.com/exchange/betting/json-rpc/v1")


def configured() -> bool:
    return all(os.environ.get(k, "").strip() for k in SECRET_ENV)


def scrub(text: str) -> str:
    for env in SECRET_ENV:
        secret = os.environ.get(env, "").strip()
        if secret:
            text = text.replace(secret, "***")
    session = _read_session().get("token")
    if session:
        text = text.replace(session, "***")
    return text


# ---------------------------------------------------------------- session store


def _read_session() -> dict[str, Any]:
    try:
        return json.loads(session_file().read_text())
    except (OSError, ValueError):
        return {}


def _write_session(data: dict[str, Any]) -> None:
    path = session_file()
    try:
        path.write_text(json.dumps(data))
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- HTTP (replaced in tests)


async def _http_post(url: str, *, data: dict | None = None, json_body: Any = None,
                     headers: dict | None = None) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, data=data, json=json_body, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:300]}
    return response.status_code, body


# ---------------------------------------------------------------- login


async def login(force: bool = False) -> dict[str, Any]:
    """Returns {'ok': True, 'token': ...} or {'ok': False, 'error': ...}. Never raises."""
    if not configured():
        return {"ok": False, "error": "Betfair is not set up. Add BETFAIR_APP_KEY, BETFAIR_USERNAME and "
                                      "BETFAIR_PASSWORD to .env (run setup.command)."}
    stored = _read_session()
    now = time.time()
    if not force and stored.get("token") and now - stored.get("created", 0) < SESSION_MAX_AGE_S:
        return {"ok": True, "token": stored["token"], "reused": True}
    if stored.get("failed_at") and now - stored["failed_at"] < LOGIN_BACKOFF_S:
        wait = int((LOGIN_BACKOFF_S - (now - stored["failed_at"])) // 60) + 1
        return {"ok": False, "error": f"Betfair login failed recently ({stored.get('failed_reason')}). "
                                      f"Not retrying for {wait} min so your account cannot get locked. "
                                      "Check the username/password in .env."}
    headers = {"X-Application": os.environ["BETFAIR_APP_KEY"].strip(), "Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded"}
    form = {"username": os.environ["BETFAIR_USERNAME"].strip(), "password": os.environ["BETFAIR_PASSWORD"]}
    try:
        status, body = await _http_post(login_url(), data=form, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Could not reach Betfair login ({type(exc).__name__}). This network may "
                                      "block betting sites (common on work laptops and VPNs)."}
    if status == 200 and isinstance(body, dict) and body.get("status") == "SUCCESS" and body.get("token"):
        _write_session({"token": body["token"], "created": now})
        return {"ok": True, "token": body["token"], "reused": False}
    reason = (body.get("error") or body.get("status") or f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
    _write_session({"failed_at": now, "failed_reason": str(reason)})
    hint = ""
    if "PASSWORD" in str(reason) or "USERNAME" in str(reason):
        hint = " Check BETFAIR_USERNAME and BETFAIR_PASSWORD in .env."
    elif "2FA" in str(reason) or "SECURITY" in str(reason).upper():
        hint = " Two-step login is on for this account; certificate login is needed (not set up yet)."
    elif "APP_KEY" in str(reason).upper():
        hint = " Check BETFAIR_APP_KEY is the 'Version 1.0-Delay' key."
    return {"ok": False, "error": scrub(f"Betfair login refused: {reason}.{hint}")}


# ---------------------------------------------------------------- read-only API calls


async def call(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send one read-only Betting API operation. Returns {'ok': True, 'result': ...} or an error dict."""
    if operation not in READ_ONLY_OPERATIONS:
        return {"ok": False, "error": f"Refused: '{operation}' is not a read-only operation. "
                                      "This connector never places or changes bets."}
    for attempt in range(2):
        session = await login(force=attempt == 1)
        if not session["ok"]:
            return session
        headers = {"X-Application": os.environ["BETFAIR_APP_KEY"].strip(), "X-Authentication": session["token"],
                   "Content-Type": "application/json", "Accept": "application/json"}
        payload = {"jsonrpc": "2.0", "method": f"SportsAPING/v1.0/{operation}", "params": params, "id": 1}
        try:
            status, body = await _http_post(api_url(), json_body=payload, headers=headers)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"Could not reach Betfair ({type(exc).__name__}). This network may "
                                          "block betting sites (common on work laptops and VPNs)."}
        if isinstance(body, dict) and "result" in body:
            return {"ok": True, "result": body["result"]}
        code = _error_code(body)
        if code in ("INVALID_SESSION_INFORMATION", "NO_SESSION") and attempt == 0:
            continue  # session expired: log in once more
        return {"ok": False, "error": scrub(f"Betfair {operation} failed: {code or 'HTTP ' + str(status)}")}
    return {"ok": False, "error": "Betfair session could not be refreshed."}


def _error_code(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    err = body.get("error") or {}
    data = err.get("data") or {}
    exc = data.get("APINGException") or data.get("AccountAPINGException") or {}
    return str(exc.get("errorCode") or err.get("message") or "")


# ---------------------------------------------------------------- shaping


def featured_keywords() -> tuple[str, ...]:
    custom = os.environ.get("FEATURED_SOCCER", "").strip()
    if custom:
        return tuple(k.strip().lower() for k in custom.split(",") if k.strip())
    return FEATURED_SOCCER


def is_featured_soccer(competition_name: str) -> bool:
    name = (competition_name or "").lower()
    if "women" in name or "u21" in name or "u19" in name or "u23" in name:
        return False
    return any(k in name for k in featured_keywords())


def fair_price(back: float | None, lay: float | None, last: float | None) -> tuple[float | None, bool]:
    """Best single estimate of a runner's price, and whether the market is thin.
    A wide back/lay gap means few bets are waiting, so the mid-point is unreliable."""
    if back and lay and lay / back <= 1.25:
        return round((back + lay) / 2, 3), False
    if last:
        return last, True
    return (back or lay), True


def split_event_name(name: str) -> tuple[str, str]:
    for sep in (" v ", " vs ", " V ", " @ "):
        if sep in name:
            a, b = name.split(sep, 1)
            return a.strip(), b.strip()
    return name.strip(), ""


def runner_name(name: str) -> str:
    return "Draw" if name.strip().lower() in ("the draw", "draw") else name.strip()


def shape_book(catalogue: dict[str, Any], book: dict[str, Any]) -> dict[str, Any]:
    """Merge one market's catalogue entry (names) and book (prices) into a plain summary."""
    names = {r["selectionId"]: runner_name(r.get("runnerName", "")) for r in catalogue.get("runners", [])}
    runners = []
    back_total = 0.0
    for r in book.get("runners", []):
        ex = r.get("ex") or {}
        back = (ex.get("availableToBack") or [{}])[0]
        lay = (ex.get("availableToLay") or [{}])[0]
        bp, lp = back.get("price"), lay.get("price")
        mid, thin = fair_price(bp, lp, r.get("lastPriceTraded"))
        if bp:
            back_total += 1 / bp
        runners.append({
            "name": names.get(r.get("selectionId"), str(r.get("selectionId"))),
            "back_price": bp, "back_size": back.get("size"),
            "lay_price": lp, "lay_size": lay.get("size"),
            "mid_price": mid,
            "implied_chance_pct": round(100 / mid, 1) if mid else None,
            "last_traded": r.get("lastPriceTraded"),
            "thin_market": thin,
            "status": r.get("status"),
        })
    active = [x for x in runners if x["mid_price"] and x["status"] == "ACTIVE"]
    fav = min(active, key=lambda x: x["mid_price"], default=None)
    event = catalogue.get("event") or {}
    return {
        "market_id": catalogue.get("marketId") or book.get("marketId"),
        "event": event.get("name"),
        "competition": (catalogue.get("competition") or {}).get("name"),
        "market": catalogue.get("marketName"),
        "start_utc": catalogue.get("marketStartTime") or event.get("openDate"),
        "status": book.get("status"),
        "in_play": book.get("inplay"),
        "favourite": fav["name"] if fav else None,
        "thin_market": any(x["thin_market"] for x in runners),
        "back_overround_pct": round((back_total - 1) * 100, 2) if back_total else None,
        "runners": runners,
    }


def as_odds_api_event(summary: dict[str, Any]) -> dict[str, Any]:
    """Present a Betfair market in The Odds API's event shape, so alerts.evaluate can read it.
    Uses the best BACK price (what you would get if you bet on the team now)."""
    home, away = split_event_name(summary.get("event") or "")
    outcomes = [{"name": r["name"], "price": r["back_price"]} for r in summary["runners"] if r.get("back_price")]
    return {
        "id": f"betfair:{summary['market_id']}",
        "home_team": home, "away_team": away,
        "commence_time": summary.get("start_utc"),
        "bookmakers": [{"key": "betfair_exchange", "title": "Betfair Exchange (back)",
                        "markets": [{"key": "h2h", "outcomes": outcomes}]}],
    }

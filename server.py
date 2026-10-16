# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0,<2", "httpx>=0.27", "python-dotenv>=1.0", "tzdata"]
# ///
"""Read-only cricket and football betting-research MCP server for Claude.

Odds      : The Odds API      https://the-odds-api.com          ODDS_API_KEY
Football  : football-data.org https://www.football-data.org     FOOTBALL_DATA_KEY  (fixtures, table, H2H, model)
            API-Football      https://www.api-football.com      API_FOOTBALL_KEY   (player stats, live stats)
Cricket   : CricketData.org   https://cricketdata.org           CRICKETDATA_API_KEY
Exchange  : Betfair (AU)      delayed app key, read-only        BETFAIR_APP_KEY / _USERNAME / _PASSWORD

Design rules
- Read-only. There is no bet-placement code in this project.
- Free-tier friendly: responses are cached, provider quota is tracked from response
  headers and calls stop before the quota runs out.
- API keys never reach the model and are removed from error text.
- All arithmetic (margins, fair odds, value, model) is done in code, not by the model.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import alerts  # noqa: E402
import betfair  # noqa: E402
import models  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"), interpolate=False)
except ImportError:  # pragma: no cover
    pass

# Local use (Claude Desktop): stdio. Cloud use (Render etc.): MCP_TRANSPORT=http serves the tools over
# HTTPS at /<MCP_PATH_SECRET>/mcp, so only someone who knows the secret URL can reach them.
HTTP_MODE = os.environ.get("MCP_TRANSPORT", "").lower() == "http"
if HTTP_MODE:
    _secret = os.environ.get("MCP_PATH_SECRET", "").strip()
    if len(_secret) < 24 or not _secret.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("Set MCP_PATH_SECRET to a random string of 24+ letters/digits before starting in http mode.")
    mcp = FastMCP("sports-odds", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                  streamable_http_path=f"/{_secret}/mcp", stateless_http=True, json_response=True)
else:
    mcp = FastMCP("sports-odds")

ODDS_BASE = "https://api.the-odds-api.com/v4"
APIFOOTBALL_BASE = "https://v3.football.api-sports.io"
FDORG_BASE = "https://api.football-data.org/v4"
CRICKET_BASE = "https://api.cricapi.com/v1"

KEY_ENV = {
    "odds": "ODDS_API_KEY",
    "football": "API_FOOTBALL_KEY",
    "fdorg": "FOOTBALL_DATA_KEY",
    "cricket": "CRICKETDATA_API_KEY",
}
DEFAULT_RESERVE = {"odds": 20, "football": 5, "fdorg": 0, "cricket": 5}

# football-data.org competition code -> The Odds API sport key
COMPETITIONS = {
    "PL": ("Premier League", "soccer_epl"),
    "ELC": ("Championship", "soccer_efl_champ"),
    "PD": ("La Liga", "soccer_spain_la_liga"),
    "BL1": ("Bundesliga", "soccer_germany_bundesliga"),
    "SA": ("Serie A", "soccer_italy_serie_a"),
    "FL1": ("Ligue 1", "soccer_france_ligue_one"),
    "DED": ("Eredivisie", "soccer_netherlands_eredivisie"),
    "PPL": ("Primeira Liga", "soccer_portugal_primeira_liga"),
    "CL": ("Champions League", "soccer_uefa_champs_league"),
    "BSA": ("Brazil Serie A", "soccer_brazil_campeonato"),
    "WC": ("World Cup", "soccer_fifa_world_cup"),
    "EC": ("European Championship", "soccer_uefa_european_championship"),
}

STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name(".state.json")))

# --------------------------------------------------------------------------
# Infrastructure: config, time zones, cache, quota, HTTP
# --------------------------------------------------------------------------

_cache: dict[Any, tuple[float, Any]] = {}
_quota: dict[str, dict[str, Any]] = {p: {} for p in KEY_ENV}


def _key(provider: str) -> str | None:
    value = os.environ.get(KEY_ENV[provider], "").strip()
    return value or None


def _reserve(provider: str) -> int:
    try:
        return int(os.environ.get(f"{provider.upper()}_RESERVE", DEFAULT_RESERVE[provider]))
    except ValueError:
        return DEFAULT_RESERVE[provider]


def _ttl(name: str, default: int) -> int:
    try:
        return int(os.environ.get(f"TTL_{name.upper()}", default))
    except ValueError:
        return default


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("LOCAL_TZ", "Australia/Brisbane"))
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _local(iso: str | None) -> str | None:
    dt = _parse_time(iso)
    if not dt:
        return None
    return dt.astimezone(_tz()).strftime("%a %d %b %Y, %I:%M %p %Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scrub(text: str) -> str:
    for env in (*KEY_ENV.values(), "MCP_PATH_SECRET"):
        secret = os.environ.get(env, "").strip()
        if secret:
            text = text.replace(secret, "***")
    return betfair.scrub(text)


def _cache_get(key: Any) -> Any | None:
    item = _cache.get(key)
    if item and item[0] > time.monotonic():
        return item[1]
    _cache.pop(key, None)
    return None


def _cache_put(key: Any, ttl: int, value: Any) -> None:
    _cache[key] = (time.monotonic() + ttl, value)


async def _http_get(url: str, params: dict | None = None, headers: dict | None = None) -> tuple[int, Any, dict[str, str]]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url, params=params, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return response.status_code, body, {k.lower(): v for k, v in response.headers.items()}


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def _update_quota(provider: str, headers: dict[str, str], body: Any) -> None:
    q = _quota[provider]
    if provider == "odds":
        remaining = _to_int(headers.get("x-requests-remaining"))
        if remaining is not None:
            q.update(remaining=remaining, used=_to_int(headers.get("x-requests-used")), last_call_cost=_to_int(headers.get("x-requests-last")))
            state = _load_state()  # shared with the background watcher
            state["odds_remaining"] = remaining
            _save_state(state)
    elif provider == "football":
        remaining = _to_int(headers.get("x-ratelimit-requests-remaining"))
        if remaining is not None:
            q.update(remaining=remaining, daily_limit=_to_int(headers.get("x-ratelimit-requests-limit")))
    elif provider == "fdorg":
        remaining = _to_int(headers.get("x-requests-available-minute"))
        if remaining is not None:
            q.update(remaining_this_minute=remaining)
    elif provider == "cricket" and isinstance(body, dict):
        info = body.get("info") or {}
        used, limit = _to_int(info.get("hitsToday")), _to_int(info.get("hitsLimit"))
        if used is not None and limit is not None:
            q.update(remaining=limit - used, used=used, daily_limit=limit)
    if q:
        q["updated_utc"] = _now().isoformat(timespec="seconds")


async def _fetch(provider: str, url: str, params: dict[str, Any] | None, headers: dict[str, str] | None,
                 ttl: int, fresh: bool = False, cost: int = 1) -> dict[str, Any]:
    """Cached, quota-guarded GET. Always returns {'ok': bool, ...}; never raises."""
    if not _key(provider):
        return {"ok": False, "error": f"{KEY_ENV[provider]} is not set in .env. See README.md."}

    cache_key = (provider, url, tuple(sorted((k, str(v)) for k, v in (params or {}).items() if k.lower() != "apikey")))
    if not fresh:
        hit = _cache_get(cache_key)
        if hit is not None:
            return {"ok": True, "cached": True, "data": hit}

    if provider == "odds" and _quota["odds"].get("remaining") is None:
        known = _load_state().get("odds_remaining")
        if known is not None:
            _quota["odds"]["remaining"] = known
    remaining = _quota[provider].get("remaining")
    if cost > 0 and remaining is not None and remaining - cost < _reserve(provider):
        return {"ok": False, "error": (
            f"Quota guard: {remaining} {provider} units left and this call costs about {cost} "
            f"(reserve {_reserve(provider)}). Wait for the reset or lower {provider.upper()}_RESERVE in .env.")}

    try:
        status, body, resp_headers = await _http_get(url, params, headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": (
            f"Could not reach {provider} ({type(exc).__name__}). If other providers work, this network "
            "may block betting/odds sites (common on work laptops and VPNs).")}

    _update_quota(provider, resp_headers, body)
    if status == 429:
        return {"ok": False, "error": f"{provider}: rate limited (HTTP 429). Try again in a minute."}
    if status >= 400:
        return {"ok": False, "error": f"{provider}: HTTP {status}", "detail": _scrub(str(body))[:300]}

    _cache_put(cache_key, ttl, body)
    return {"ok": True, "cached": False, "data": body}


# --------------------------------------------------------------------------
# Odds helpers (The Odds API)
# --------------------------------------------------------------------------


def _outcome_label(outcome: dict[str, Any]) -> str:
    label = str(outcome.get("name"))
    if outcome.get("description"):
        label = f"{outcome['description']} {label}"
    if "point" in outcome:
        label = f"{label} {outcome['point']}"
    return label


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    start = _parse_time(event.get("commence_time"))
    markets: dict[str, dict[str, dict[str, float]]] = {}
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            for outcome in market.get("outcomes", []):
                markets.setdefault(market["key"], {}).setdefault(_outcome_label(outcome), {})[book["title"]] = outcome["price"]
    return {
        "event_id": event.get("id"),
        "sport_key": event.get("sport_key"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "commence_time_utc": event.get("commence_time"),
        "local_time": _local(event.get("commence_time")),
        "in_play": bool(start and start <= _now()),
        "markets": markets,
    }


def _credit_cost(markets: str, regions: str, bookmakers: str) -> int:
    n_markets = max(1, len([m for m in markets.split(",") if m.strip()]))
    if bookmakers.strip():
        n_regions = max(1, math.ceil(len([b for b in bookmakers.split(",") if b.strip()]) / 10))
    else:
        n_regions = max(1, len([r for r in regions.split(",") if r.strip()]))
    return n_markets * n_regions


async def _odds_raw(sport_key: str, regions: str, markets: str, bookmakers: str, event_id: str, fresh: bool) -> dict[str, Any]:
    params: dict[str, Any] = {"apiKey": _key("odds"), "markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"}
    if bookmakers.strip():
        params["bookmakers"] = bookmakers
    else:
        params["regions"] = regions
    path = f"/sports/{sport_key}/events/{event_id}/odds" if event_id else f"/sports/{sport_key}/odds"
    cost = _credit_cost(markets, regions, bookmakers)
    result = await _fetch("odds", ODDS_BASE + path, params, None, _ttl("odds", 120), fresh, cost)
    if result["ok"]:
        data = result["data"]
        result["events"] = [data] if isinstance(data, dict) else data
        result["estimated_cost_credits"] = 0 if result["cached"] else cost
    return result


async def _odds_events(sport_key: str, fresh: bool = False) -> dict[str, Any]:
    return await _fetch("odds", f"{ODDS_BASE}/sports/{sport_key}/events", {"apiKey": _key("odds")}, None,
                        _ttl("events", 300), fresh, cost=0)


async def _odds_sports(include_inactive: bool = False) -> dict[str, Any]:
    params = {"apiKey": _key("odds"), "all": "true" if include_inactive else "false"}
    return await _fetch("odds", f"{ODDS_BASE}/sports", params, None, _ttl("static", 3600), cost=0)


# --------------------------------------------------------------------------
# Status tools
# --------------------------------------------------------------------------


@mcp.tool()
async def quota_status() -> dict[str, Any]:
    """Which API keys are configured, remaining free-tier quota per provider, alerts count, local time zone."""
    return {
        "keys_configured": {**{p: bool(_key(p)) for p in KEY_ENV}, "betfair": betfair.configured()},
        "quota": _quota,
        "odds_remaining_last_seen": _load_state().get("odds_remaining"),
        "reserve": {p: _reserve(p) for p in KEY_ENV},
        "local_time_zone": str(_tz()),
        "active_alerts": len([a for a in alerts.load() if not a.get("triggered")]),
        "cached_responses": len(_cache),
        "notes": [
            "odds: monthly credits; a call costs markets x regions. Listing sports and events is free.",
            "football-data.org: 10 requests/minute on the free plan.",
            "API-Football: 100 requests/day, resets 00:00 UTC.",
            "CricketData: 100 hits/day.",
        ],
    }


@mcp.tool()
async def clear_cache() -> dict[str, Any]:
    """Forget cached responses so the next calls fetch fresh data (this uses quota)."""
    count = len(_cache)
    _cache.clear()
    return {"cleared": count}


# --------------------------------------------------------------------------
# Odds tools
# --------------------------------------------------------------------------


@mcp.tool()
async def odds_list_sports(group: str = "", include_inactive: bool = False) -> dict[str, Any]:
    """List sport keys in The Odds API (FREE, no credits). group='Cricket' or 'Soccer' filters.
    Keys like cricket_ipl, cricket_test_match, soccer_epl are used by the other odds tools."""
    result = await _odds_sports(include_inactive)
    if not result["ok"]:
        return result
    rows = [{"key": s["key"], "group": s["group"], "title": s["title"], "active": s["active"]}
            for s in result["data"] if not group or group.lower() in s["group"].lower()]
    return {"ok": True, "cached": result["cached"], "count": len(rows), "sports": rows}


@mcp.tool()
async def odds_list_events(sport_key: str, fresh: bool = False) -> dict[str, Any]:
    """Live and upcoming events for a sport key with local start times and event_id (FREE)."""
    result = await _odds_events(sport_key, fresh)
    if not result["ok"]:
        return result
    rows = []
    for e in result["data"]:
        start = _parse_time(e.get("commence_time"))
        rows.append({"event_id": e["id"], "home_team": e["home_team"], "away_team": e["away_team"],
                     "local_time": _local(e.get("commence_time")), "in_play": bool(start and start <= _now())})
    return {"ok": True, "cached": result["cached"], "count": len(rows), "events": rows}


@mcp.tool()
async def odds_get(sport_key: str, regions: str = "au", markets: str = "h2h", bookmakers: str = "",
                   event_id: str = "", fresh: bool = False) -> dict[str, Any]:
    """Bookmaker odds (decimal) for a sport, or one event if event_id is given.
    COSTS credits = markets x regions (cached repeats are free). regions: au, uk, eu, us, us2.
    markets: h2h (match winner / 1X2), totals, spreads. With event_id, any market the books offer.
    bookmakers: optional comma list such as 'sportsbet,tab,pinnacle,betfair_ex_au' (overrides regions)."""
    result = await _odds_raw(sport_key, regions, markets, bookmakers, event_id, fresh)
    if not result["ok"]:
        return result
    events = [compact_event(e) for e in result["events"]]
    return {"ok": True, "cached": result["cached"], "estimated_cost_credits": result["estimated_cost_credits"],
            "credits_remaining": _quota["odds"].get("remaining"), "count": len(events), "events": events}


@mcp.tool()
async def odds_compare(sport_key: str, market: str = "h2h", regions: str = "au", bookmakers: str = "",
                       event_id: str = "", team: str = "", fresh: bool = False) -> dict[str, Any]:
    """Compare bookmakers on one market: best price and who offers it, average, each book's margin,
    consensus fair probability/odds (margin removed), edge of the best price vs fair, favourite,
    and whether best prices form an arbitrage. team='Liverpool' keeps only that team's matches.
    Costs 1 credit per region (free if cached)."""
    result = await _odds_raw(sport_key, regions, market, bookmakers, event_id, fresh)
    if not result["ok"]:
        return result
    out = []
    for event in (compact_event(e) for e in result["events"]):
        if team and not any(models.same_team(t, team) for t in (event["home_team"], event["away_team"])):
            continue
        labels = event["markets"].get(market)
        if not labels:
            continue
        out.append({"event_id": event["event_id"], "match": f"{event['home_team']} v {event['away_team']}",
                    "local_time": event["local_time"], "in_play": event["in_play"], "market": market,
                    **models.analyse_market(labels)})
    return {"ok": True, "cached": result["cached"], "estimated_cost_credits": result["estimated_cost_credits"],
            "credits_remaining": _quota["odds"].get("remaining"), "count": len(out), "comparisons": out}


@mcp.tool()
async def odds_scores(sport_key: str, days_from: int = 0, fresh: bool = False) -> dict[str, Any]:
    """Live and recent scores from The Odds API (selected leagues). days_from 1-3 adds completed games.
    Costs 1 credit (2 with days_from)."""
    params: dict[str, Any] = {"apiKey": _key("odds")}
    cost = 1
    if days_from:
        params["daysFrom"] = max(1, min(3, days_from))
        cost = 2
    result = await _fetch("odds", f"{ODDS_BASE}/sports/{sport_key}/scores", params, None, _ttl("scores", 60), fresh, cost)
    if not result["ok"]:
        return result
    return {"ok": True, "cached": result["cached"], "scores": result["data"]}


@mcp.tool()
async def odds_math(decimal_odds: list[float], stake: float = 0.0, my_probability_pct: float = 0.0) -> dict[str, Any]:
    """Pure calculation, no API call. Give decimal odds for ALL outcomes of one market.
    Returns implied and fair (no-margin) probabilities, fair odds, the margin, payouts for a stake,
    and the edge if you supply your own probability for the FIRST outcome."""
    if len(decimal_odds) < 2 or any(o <= 1.0 for o in decimal_odds):
        return {"ok": False, "error": "Provide at least two decimal odds, each greater than 1.0."}
    fair = models.no_vig_probs(decimal_odds)
    rows = []
    for odds, p in zip(decimal_odds, fair):
        row = {"odds": odds, "implied_prob_pct": round(models.implied_prob(odds) * 100, 2),
               "fair_prob_pct": round(p * 100, 2), "fair_odds": round(1 / p, 3)}
        if stake:
            row["payout"] = round(stake * odds, 2)
            row["profit"] = round(stake * (odds - 1), 2)
        rows.append(row)
    out: dict[str, Any] = {"ok": True, "margin_pct": round(models.overround_pct(decimal_odds), 2), "outcomes": rows}
    if my_probability_pct:
        out["edge_first_outcome_pct"] = round(models.edge_pct(my_probability_pct / 100, decimal_odds[0]), 2)
    return out


# --------------------------------------------------------------------------
# football-data.org: fixtures, table, head-to-head, model input
# --------------------------------------------------------------------------


async def _fd(path: str, params: dict[str, Any] | None, ttl: int, fresh: bool = False) -> dict[str, Any]:
    clean = {k: v for k, v in (params or {}).items() if v not in ("", None, 0)}
    return await _fetch("fdorg", FDORG_BASE + path, clean, {"X-Auth-Token": _key("fdorg") or ""}, ttl, fresh, cost=0)


def _fd_match_row(m: dict[str, Any]) -> dict[str, Any]:
    ft = (m.get("score") or {}).get("fullTime") or {}
    return {
        "match_id": m.get("id"),
        "date": m.get("utcDate"),
        "local_time": _local(m.get("utcDate")),
        "status": m.get("status"),
        "matchday": m.get("matchday"),
        "home": (m.get("homeTeam") or {}).get("name"),
        "away": (m.get("awayTeam") or {}).get("name"),
        "score": f"{ft.get('home')}-{ft.get('away')}" if ft.get("home") is not None else None,
    }


def _results_from(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in matches:
        ft = (m.get("score") or {}).get("fullTime") or {}
        if m.get("status") == "FINISHED" and ft.get("home") is not None:
            out.append({"home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"], "home_goals": ft["home"],
                        "away_goals": ft["away"], "date": m.get("utcDate", "")})
    out.sort(key=lambda r: r["date"])
    return out


async def _season_results(competition: str) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    cur = await _fd(f"/competitions/{competition}/matches", {"status": "FINISHED"}, _ttl("stats", 3600))
    if not cur["ok"]:
        return [], [cur["error"]]
    matches = cur["data"].get("matches", [])
    results = _results_from(matches)
    if len(results) < 80 and matches:
        start = ((matches[0].get("season") or {}).get("startDate") or "")[:4]
        if start.isdigit():
            prev = await _fd(f"/competitions/{competition}/matches", {"season": int(start) - 1, "status": "FINISHED"},
                             _ttl("static", 86400))
            if prev["ok"]:
                results = _results_from(prev["data"].get("matches", [])) + results
                notes.append(f"Model also uses last season ({int(start) - 1}) because this season has few games.")
            else:
                notes.append("Last season not available on this plan; model uses this season only.")
    return results, notes


@mcp.tool()
async def football_fixtures(competition: str = "PL", status: str = "SCHEDULED,TIMED", team: str = "",
                            days_ahead: int = 14) -> dict[str, Any]:
    """Fixtures or results with local kick-off times (football-data.org, free key).
    competition codes: PL, ELC, PD (La Liga), BL1, SA, FL1, DED, PPL, CL, BSA, WC, EC.
    status: SCHEDULED,TIMED for upcoming; FINISHED for results; IN_PLAY for live. team filters by name."""
    params: dict[str, Any] = {"status": status}
    if "SCHEDULED" in status or "TIMED" in status:
        params["dateFrom"] = _now().date().isoformat()
        params["dateTo"] = (_now().date() + timedelta(days=days_ahead)).isoformat()
    r = await _fd(f"/competitions/{competition}/matches", params, _ttl("fixtures", 600))
    if not r["ok"]:
        return r
    rows = [_fd_match_row(m) for m in r["data"].get("matches", [])]
    if team:
        rows = [x for x in rows if models.same_team(x["home"] or "", team) or models.same_team(x["away"] or "", team)]
    return {"ok": True, "cached": r["cached"], "count": len(rows), "matches": rows}


@mcp.tool()
async def football_table(competition: str = "PL") -> dict[str, Any]:
    """League table with position, points, W/D/L, goals and recent form (football-data.org)."""
    r = await _fd(f"/competitions/{competition}/standings", None, _ttl("stats", 1800))
    if not r["ok"]:
        return r
    total = next((s for s in r["data"].get("standings", []) if s.get("type") == "TOTAL"), None)
    rows = [{"pos": t["position"], "team": t["team"]["name"], "played": t["playedGames"], "won": t["won"],
             "drawn": t["draw"], "lost": t["lost"], "gf": t["goalsFor"], "ga": t["goalsAgainst"],
             "gd": t["goalDifference"], "points": t["points"], "form": t.get("form")}
            for t in (total or {}).get("table", [])]
    return {"ok": True, "cached": r["cached"], "table": rows}


@mcp.tool()
async def match_preview(home: str, away: str, competition: str = "PL", regions: str = "au",
                        include_odds: bool = True, fresh_odds: bool = False) -> dict[str, Any]:
    """ONE-CALL football betting preview, e.g. home='Liverpool', away='Man City'.
    Returns kick-off (local time), league positions, recent form, head-to-head record,
    a simple goals model (expected goals, win/draw/loss %, over 2.5, BTTS, fair odds),
    live bookmaker prices (best price per outcome, margins) and a value check comparing
    each best price with (a) the market's own fair probability and (b) the model.
    Odds cost 1 credit per region (cached 2 min). Use this for 'who should I back / are the odds fair'.
    The market consensus is usually the sharper estimate; treat the model as a sanity check."""
    notes: list[str] = []
    out: dict[str, Any] = {"ok": True, "home": home, "away": away, "competition": competition}
    comp = COMPETITIONS.get(competition.upper())
    if not comp:
        return {"ok": False, "error": f"Unknown competition. Use one of {', '.join(COMPETITIONS)}."}

    # 1. Fixture (football-data.org)
    fixture = None
    fx = await _fd(f"/competitions/{competition}/matches", {"status": "SCHEDULED,TIMED,IN_PLAY,PAUSED"}, _ttl("fixtures", 600))
    if fx["ok"]:
        for m in fx["data"].get("matches", []):
            if models.same_team(m["homeTeam"]["name"], home) and models.same_team(m["awayTeam"]["name"], away):
                fixture = m
                break
        if not fixture:  # maybe the user swapped home and away
            for m in fx["data"].get("matches", []):
                if models.same_team(m["homeTeam"]["name"], away) and models.same_team(m["awayTeam"]["name"], home):
                    fixture = m
                    home, away = away, home
                    notes.append("Home and away were swapped to match the fixture list.")
                    break
        out["fixture"] = _fd_match_row(fixture) if fixture else None
        if not fixture:
            notes.append("No upcoming fixture found between these teams in this competition.")
    else:
        notes.append(f"Fixtures/stats unavailable: {fx['error']}")

    # 2. Results -> form + model
    results, model_notes = await _season_results(competition) if fx["ok"] else ([], [])
    notes += model_notes
    if results:
        out["form_last5"] = {home: models.form_string(results, home), away: models.form_string(results, away)}
        model = models.fit_strengths(results)
        pred = models.predict_match(model, home, away)
        out["model"] = pred
        if not pred:
            notes.append("Model could not find one of the teams in the results data.")

    # 3. Table positions
    table = await football_table(competition) if fx["ok"] else {"ok": False}
    if table.get("ok"):
        pos = {}
        for row in table["table"]:
            for t in (home, away):
                if models.same_team(row["team"], t):
                    pos[t] = {k: row[k] for k in ("pos", "points", "played", "gd", "form")}
        out["table"] = pos

    # 4. Head-to-head
    if fixture:
        h2h = await _fd(f"/matches/{fixture['id']}/head2head", {"limit": 10}, _ttl("stats", 3600))
        if h2h["ok"]:
            agg = h2h["data"].get("aggregates") or {}
            out["head_to_head"] = {
                "matches": agg.get("numberOfMatches"),
                "total_goals": agg.get("totalGoals"),
                home: {k: (agg.get("homeTeam") or {}).get(k) for k in ("wins", "draws", "losses")},
                away: {k: (agg.get("awayTeam") or {}).get(k) for k in ("wins", "draws", "losses")},
                "recent": [_fd_match_row(m) for m in h2h["data"].get("matches", [])][:6],
            }
        else:
            notes.append(f"Head-to-head unavailable: {h2h['error']}")

    # 5. Odds + value check
    if include_odds:
        odds = await _odds_raw(comp[1], regions, "h2h", "", "", fresh_odds)
        if odds["ok"]:
            event = next((e for e in odds["events"] if models.same_team(e["home_team"], home)
                          and models.same_team(e["away_team"], away)), None)
            if event:
                labels = compact_event(event)["markets"].get("h2h", {})
                analysis = models.analyse_market(labels)
                out["odds"] = {"local_time": _local(event["commence_time"]), "event_id": event["id"],
                               "credits_used": odds["estimated_cost_credits"],
                               "credits_remaining": _quota["odds"].get("remaining"), **analysis}
                value = {}
                model_probs = (out.get("model") or {}).get("prob_pct") or {}
                for label, o in analysis["outcomes"].items():
                    side = "draw" if label.lower() == "draw" else "home" if models.same_team(label, home) else "away"
                    row = {"best_price": o["best_price"], "best_bookmaker": o["best_bookmaker"],
                           "market_fair_odds": o.get("consensus_fair_odds"),
                           "edge_vs_market_pct": o.get("best_price_edge_vs_consensus_pct")}
                    if side in model_probs:
                        row["model_fair_odds"] = round(100 / model_probs[side], 2)
                        row["edge_vs_model_pct"] = round(models.edge_pct(model_probs[side] / 100, o["best_price"]), 2)
                    e_mkt, e_mod = row["edge_vs_market_pct"] or 0, row.get("edge_vs_model_pct")
                    if e_mkt > 0 and (e_mod is None or e_mod > 0):
                        row["verdict"] = "Best price is above fair value"
                    elif e_mkt > -2:
                        row["verdict"] = "Roughly fair (within normal margin)"
                    else:
                        row["verdict"] = "Below fair value"
                    value[label] = row
                out["value_check"] = value
            else:
                notes.append("Match not listed by bookmakers yet in this region.")
        else:
            notes.append(f"Odds unavailable: {odds['error']}")

    out["notes"] = notes
    out["disclaimer"] = "Research aid, not financial advice. Bet only what you can afford to lose."
    return out


# --------------------------------------------------------------------------
# API-Football: player and live match stats (optional key)
# --------------------------------------------------------------------------


async def _apifootball(path: str, params: dict[str, Any], ttl: int, fresh: bool) -> dict[str, Any]:
    clean = {k: v for k, v in params.items() if v not in ("", 0, None)}
    result = await _fetch("football", APIFOOTBALL_BASE + path, clean, {"x-apisports-key": _key("football") or ""}, ttl, fresh)
    if not result["ok"]:
        return result
    body = result["data"]
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        return {"ok": False, "error": "API-Football error", "detail": _scrub(str(errors))[:300]}
    return {"ok": True, "cached": result["cached"], "response": body.get("response", []), "paging": body.get("paging")}


def _af_fixture(item: dict[str, Any]) -> dict[str, Any]:
    fx, teams, goals, league = item["fixture"], item["teams"], item["goals"], item["league"]
    return {"fixture_id": fx["id"], "local_time": _local(fx["date"]), "status": fx["status"].get("long"),
            "minute": fx["status"].get("elapsed"), "league": f"{league.get('name')} ({league.get('country')})",
            "home": teams["home"]["name"], "away": teams["away"]["name"],
            "score": f"{goals.get('home')}-{goals.get('away')}"}


@mcp.tool()
async def football_live(league_id: int = 0, fresh: bool = False) -> dict[str, Any]:
    """Football matches in play right now, all leagues or one league_id (API-Football, 1 request, cached 30s).
    Premier League is league_id 39."""
    r = await _apifootball("/fixtures", {"live": str(league_id) if league_id else "all"}, _ttl("live", 30), fresh)
    if not r["ok"]:
        return r
    return {"ok": True, "cached": r["cached"], "count": len(r["response"]), "matches": [_af_fixture(i) for i in r["response"]]}


@mcp.tool()
async def football_live_match_stats(fixture_id: int, fresh: bool = False) -> dict[str, Any]:
    """Live/finished match detail from API-Football: events (goals, cards, subs), shots, possession, corners."""
    r = await _apifootball("/fixtures", {"id": fixture_id}, _ttl("live", 30), fresh)
    if not r["ok"]:
        return r
    if not r["response"]:
        return {"ok": False, "error": "Fixture not found."}
    item = r["response"][0]
    stats = {s["team"]["name"]: {x["type"]: x["value"] for x in s.get("statistics", [])} for s in item.get("statistics", [])}
    events = [{"minute": e["time"].get("elapsed"), "team": e["team"]["name"], "player": (e.get("player") or {}).get("name"),
               "type": e["type"], "detail": e.get("detail")} for e in item.get("events", [])]
    return {"ok": True, "cached": r["cached"], "match": _af_fixture(item), "events": events, "statistics": stats}


@mcp.tool()
async def football_player_stats(season: int, search: str = "", player_id: int = 0, team_id: int = 0,
                                league_id: int = 39, fresh: bool = False) -> dict[str, Any]:
    """Player season stats (API-Football): apps, minutes, rating, goals, assists, shots, key passes, cards.
    search needs 4+ characters. The free plan only covers some seasons; errors are passed back."""
    params = {"season": season, "search": search, "id": player_id, "team": team_id, "league": league_id}
    r = await _apifootball("/players", params, _ttl("stats", 3600), fresh)
    if not r["ok"]:
        return r
    players = []
    for item in r["response"]:
        p = item["player"]
        comps = [{"team": s["team"]["name"], "league": s["league"]["name"], "apps": s["games"].get("appearences"),
                  "minutes": s["games"].get("minutes"), "rating": s["games"].get("rating"), "goals": s["goals"].get("total"),
                  "assists": s["goals"].get("assists"), "shots_on": s["shots"].get("on"), "key_passes": s["passes"].get("key"),
                  "yellow": s["cards"].get("yellow"), "red": s["cards"].get("red")} for s in item.get("statistics", [])]
        players.append({"player_id": p["id"], "name": p["name"], "age": p.get("age"), "competitions": comps})
    return {"ok": True, "cached": r["cached"], "players": players}


# --------------------------------------------------------------------------
# Cricket (CricketData.org + The Odds API events for times)
# --------------------------------------------------------------------------


async def _cricket(endpoint: str, params: dict[str, Any], ttl: int, fresh: bool = False) -> dict[str, Any]:
    q = {"apikey": _key("cricket"), **{k: v for k, v in params.items() if v not in ("", None)}}
    result = await _fetch("cricket", f"{CRICKET_BASE}/{endpoint}", q, None, ttl, fresh)
    if not result["ok"]:
        return result
    body = result["data"]
    if isinstance(body, dict) and body.get("status") == "failure":
        return {"ok": False, "error": "CricketData error", "detail": _scrub(str(body.get("reason")))[:300]}
    return {"ok": True, "cached": result["cached"], "data": body.get("data"), "hits": body.get("info", {})}


def _cricket_row(m: dict[str, Any]) -> dict[str, Any]:
    when = m.get("dateTimeGMT")
    if when and not when.endswith("Z") and "+" not in when:
        when = when + "Z"
    return {"match_id": m.get("id"), "name": m.get("name"), "type": m.get("matchType"), "status": m.get("status"),
            "venue": m.get("venue"), "local_time": _local(when) or m.get("date"), "teams": m.get("teams"),
            "score": [f"{s.get('inning')}: {s.get('r')}/{s.get('w')} ({s.get('o')} ov)" for s in m.get("score", []) or []],
            "started": m.get("matchStarted"), "ended": m.get("matchEnded"), "_utc": when}


@mcp.tool()
async def cricket_schedule(day: str = "today", include_odds_listing: bool = True) -> dict[str, Any]:
    """Cricket matches on a day in YOUR local time zone: day='today', 'tomorrow' or 'YYYY-MM-DD'.
    Merges CricketData fixtures (2 hits, cached 10 min) with matches bookmakers list in The Odds API
    (FREE, gives the sport_key and event_id needed for odds and alerts)."""
    tz = _tz()
    today = _now().astimezone(tz).date()
    target = today if day == "today" else today + timedelta(days=1) if day == "tomorrow" else date_cls.fromisoformat(day)
    notes: list[str] = []
    rows: list[dict[str, Any]] = []

    seen: set[str] = set()
    for offset in (0, 25):
        r = await _cricket("matches", {"offset": offset}, _ttl("fixtures", 600))
        if not r["ok"]:
            notes.append(f"CricketData: {r['error']}")
            break
        page = r["data"] or []
        for m in page:
            row = _cricket_row(m)
            dt = _parse_time(row["_utc"])
            if row["match_id"] not in seen and dt and dt.astimezone(tz).date() == target:
                seen.add(row["match_id"])
                rows.append(row)
        if len(page) < 25:
            break

    bettable = []
    if include_odds_listing:
        sports = await _odds_sports()
        if sports["ok"]:
            for s in (x for x in sports["data"] if x["group"] == "Cricket" and not x.get("has_outrights")):
                ev = await _odds_events(s["key"])
                if not ev["ok"]:
                    continue
                for e in ev["data"]:
                    dt = _parse_time(e.get("commence_time"))
                    if dt and dt.astimezone(tz).date() == target:
                        bettable.append({"competition": s["title"], "sport_key": s["key"], "event_id": e["id"],
                                         "match": f"{e['home_team']} v {e['away_team']}", "local_time": _local(e["commence_time"])})
        else:
            notes.append(f"Odds listing: {sports['error']}")

    for row in rows:
        row.pop("_utc", None)
        for b in bettable:
            if row.get("teams") and all(any(models.same_team(t, x) for x in b["match"].split(" v ")) for t in row["teams"]):
                row["odds_sport_key"], row["odds_event_id"] = b["sport_key"], b["event_id"]
    rows.sort(key=lambda x: x.get("local_time") or "")
    return {"ok": True, "date": target.isoformat(), "time_zone": str(tz), "matches": rows,
            "bookmaker_listed_matches": sorted(bettable, key=lambda x: x["local_time"] or ""), "notes": notes}


@mcp.tool()
async def cricket_current_matches(fresh: bool = False) -> dict[str, Any]:
    """Live and just-finished cricket matches with scores (CricketData, 1 hit, cached 60s)."""
    r = await _cricket("currentMatches", {"offset": 0}, _ttl("cricket_live", 60), fresh)
    if not r["ok"]:
        return r
    matches = [_cricket_row(m) for m in (r["data"] or [])]
    for m in matches:
        m.pop("_utc", None)
    return {"ok": True, "cached": r["cached"], "hits_today": r["hits"].get("hitsToday"), "count": len(matches), "matches": matches}


@mcp.tool()
async def cricket_scorecard(match_id: str, fresh: bool = False) -> dict[str, Any]:
    """Batting/bowling scorecard for a match (CricketData; may cost extra hits; not every match)."""
    r = await _cricket("match_scorecard", {"id": match_id}, _ttl("cricket_live", 60), fresh)
    if not r["ok"]:
        return r
    return {"ok": True, "cached": r["cached"], "scorecard": r["data"]}


@mcp.tool()
async def cricket_player(name: str) -> dict[str, Any]:
    """Cricketer profile and career stats by name (CricketData, 2 hits, cached a day)."""
    s = await _cricket("players", {"search": name, "offset": 0}, _ttl("static", 86400))
    if not s["ok"]:
        return s
    if not s["data"]:
        return {"ok": False, "error": "No player found."}
    info = await _cricket("players_info", {"id": s["data"][0]["id"]}, _ttl("static", 86400))
    if not info["ok"]:
        return info
    return {"ok": True, "other_matches": [p.get("name") for p in s["data"][1:6]], "player": info["data"]}


# --------------------------------------------------------------------------
# Betfair Exchange (read-only, delayed key) and in-play money maths
# --------------------------------------------------------------------------

BF_DELAY_NOTE = "Betfair delayed key: prices can be 1 to 180 seconds old. Back = bet it wins; lay = bet it does not."


def _bf_sport(sport: str) -> str | None:
    sport = sport.lower().strip()
    if sport.startswith("cricket"):
        return "cricket"
    if sport.startswith("soccer") or sport.startswith("football"):
        return "soccer"
    return None


async def _bf_featured_competitions(start: str, end: str) -> dict[str, Any]:
    """Betfair competition ids for the featured soccer competitions (cached 6 hours)."""
    cache_key = ("betfair-comps", start[:13], end[:13])
    hit = _cache_get(cache_key)
    if hit is not None:
        return {"ok": True, "result": hit}
    r = await betfair.call("listCompetitions", {"filter": {"eventTypeIds": ["1"],
                                                           "marketStartTime": {"from": start, "to": end}}})
    if not r["ok"]:
        return r
    comps = [{"id": c["competition"]["id"], "name": c["competition"]["name"]} for c in r["result"]
             if betfair.is_featured_soccer(c.get("competition", {}).get("name", ""))]
    _cache_put(cache_key, _ttl("betfair_competitions", 6 * 3600), comps)
    return {"ok": True, "result": comps}


async def _bf_catalogue(sport: str, hours_ahead: float = 48, in_play_only: bool = False,
                        market_type: str = "MATCH_ODDS", featured_only: bool = False) -> dict[str, Any]:
    kind = _bf_sport(sport)
    if not kind:
        return {"ok": False, "error": "sport must be cricket or soccer"}
    featured_only = featured_only and kind == "soccer"
    cache_key = ("betfair-cat", kind, int(hours_ahead), in_play_only, market_type, featured_only)
    hit = _cache_get(cache_key)
    if hit is not None:
        return {"ok": True, "result": hit, "cached": True}
    start = (_now() - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")  # include matches already in play
    end = (_now() + timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market_filter: dict[str, Any] = {"eventTypeIds": [betfair.EVENT_TYPES[kind]], "marketTypeCodes": [market_type],
                                     "marketStartTime": {"from": start, "to": end}}
    if featured_only:
        comps = await _bf_featured_competitions(start, end)
        if not comps["ok"]:
            return comps
        if not comps["result"]:
            _cache_put(cache_key, _ttl("betfair_catalogue", 300), [])
            return {"ok": True, "result": []}
        market_filter["competitionIds"] = [c["id"] for c in comps["result"]]
    if in_play_only:
        market_filter["inPlayOnly"] = True
    r = await betfair.call("listMarketCatalogue", {
        "filter": market_filter, "maxResults": 200, "sort": "FIRST_TO_START",
        "marketProjection": ["EVENT", "COMPETITION", "RUNNER_DESCRIPTION", "MARKET_START_TIME"]})
    if r["ok"]:
        _cache_put(cache_key, _ttl("betfair_catalogue", 300), r["result"])
    return r


async def _bf_books(market_ids: list[str]) -> dict[str, Any]:
    books: list[dict[str, Any]] = []
    for i in range(0, len(market_ids), 10):  # stay well under Betfair's request weight limit
        chunk = market_ids[i:i + 10]
        cache_key = ("betfair-book", tuple(chunk))
        hit = _cache_get(cache_key)
        if hit is None:
            r = await betfair.call("listMarketBook", {"marketIds": chunk,
                                                      "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}})
            if not r["ok"]:
                return r
            hit = r["result"]
            _cache_put(cache_key, _ttl("betfair_prices", 20), hit)
        books.extend(hit)
    return {"ok": True, "result": books}


def _bf_matches_team(cat: dict[str, Any], team: str) -> bool:
    names = [r.get("runnerName", "") for r in cat.get("runners", [])]
    names += [n for n in betfair.split_event_name((cat.get("event") or {}).get("name", "")) if n]
    return any(models.same_team(n, team) for n in names)


async def _bf_summaries(sport: str, team: str = "", opponent: str = "", hours_ahead: float = 48,
                        in_play_only: bool = False, market_type: str = "MATCH_ODDS",
                        limit: int = 10, featured_only: bool = False) -> dict[str, Any]:
    cat = await _bf_catalogue(sport, hours_ahead, in_play_only, market_type, featured_only)
    if not cat["ok"]:
        return cat
    markets = cat["result"]
    if team:
        markets = [m for m in markets if _bf_matches_team(m, team)]
    if opponent:
        markets = [m for m in markets if _bf_matches_team(m, opponent)]
    markets = markets[:limit]
    if not markets:
        return {"ok": True, "markets": []}
    books = await _bf_books([m["marketId"] for m in markets])
    if not books["ok"]:
        return books
    by_id = {b["marketId"]: b for b in books["result"]}
    out = []
    for m in markets:
        b = by_id.get(m["marketId"])
        if not b or b.get("status") == "CLOSED":
            continue
        started = _parse_time(m.get("marketStartTime"))
        if b.get("status") == "SUSPENDED" and not b.get("inplay") and started and started < _now():
            continue  # finished or abandoned match still listed
        if b:
            summary = betfair.shape_book(m, b)
            summary["local_time"] = _local(summary.get("start_utc"))
            out.append(summary)
    return {"ok": True, "markets": out}


@mcp.tool()
async def betfair_status() -> dict[str, Any]:
    """Check the Betfair connection (login with the delayed key). Read-only: it can never place bets."""
    if not betfair.configured():
        return {"ok": False, "configured": False,
                "error": "Add BETFAIR_APP_KEY, BETFAIR_USERNAME and BETFAIR_PASSWORD with setup.command."}
    r = await betfair.call("listEventTypes", {"filter": {"eventTypeIds": ["1", "4"]}})
    if not r["ok"]:
        return {"ok": False, "configured": True, "error": r["error"]}
    return {"ok": True, "configured": True, "sports_visible": [e["eventType"]["name"] for e in r["result"]],
            "note": BF_DELAY_NOTE}


@mcp.tool()
async def betfair_find_markets(sport: str, team: str = "", hours_ahead: float = 48, in_play_only: bool = False,
                               market_type: str = "MATCH_ODDS", limit: int = 10,
                               featured_only: bool = True) -> dict[str, Any]:
    """Betfair Exchange markets with live back/lay prices for cricket or soccer.
    sport: 'cricket' or 'soccer'. team: optional filter (e.g. 'India', 'Liverpool').
    featured_only (soccer): only EPL, Champions/Europa/Conference League, Bundesliga, La Liga, Ligue 1,
    Serie A, major cups and internationals. Set False to see every league.
    market_type: MATCH_ODDS (default), OVER_UNDER_25, BOTH_TEAMS_TO_SCORE, CORRECT_SCORE, etc.
    thin_market=True means few bets are waiting, so the price is less reliable. Free; no credits used."""
    r = await _bf_summaries(sport, team, "", hours_ahead, in_play_only, market_type, max(1, min(limit, 30)),
                            featured_only=featured_only and not team)
    if r["ok"]:
        r["note"] = BF_DELAY_NOTE
    return r


@mcp.tool()
async def betfair_match_odds(team: str, opponent: str = "", sport: str = "cricket") -> dict[str, Any]:
    """Betfair Exchange match-winner prices for one match, found by team name (and opponent if given).
    Use during a match for 'what's the price now / who's favourite now'. sport: 'cricket' or 'soccer'."""
    r = await _bf_summaries(sport, team, opponent, hours_ahead=72, limit=3)
    if r["ok"] and not r["markets"]:
        return {"ok": False, "error": f"No Betfair {sport} match-odds market found for {team}"
                                      f"{' v ' + opponent if opponent else ''} in the next 72 hours."}
    if r["ok"]:
        r["note"] = BF_DELAY_NOTE
    return r


@mcp.tool()
async def hedge_calculator(back_stake: float, back_odds: float, lay_odds: float, commission_pct: float = 5.0,
                           back_on_betfair: bool = False) -> dict[str, Any]:
    """Lock in a result: the Betfair lay stake that gives the same profit (or loss) whatever happens.
    back_stake/back_odds = your existing bet; lay_odds = current Betfair lay price for the same team.
    Set back_on_betfair=True if the original bet is also on Betfair. Maths only; places nothing."""
    try:
        return {"ok": True, **models.hedge(back_stake, back_odds, lay_odds, commission_pct, back_on_betfair)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def cash_out_check(stake: float, odds_taken: float, cash_out_offer: float, current_odds: float = 0.0,
                         my_win_chance_pct: float = 0.0) -> dict[str, Any]:
    """Is a cash-out offer fair? Compares the offer with what the bet is worth now
    (win chance x full payout). Give current_odds (from the app or betfair_match_odds) or a win chance."""
    try:
        return {"ok": True, **models.cash_out_check(stake, odds_taken, cash_out_offer, current_odds, my_win_chance_pct)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _alerts_via_betfair(sport: str, group: list[dict[str, Any]], window: timedelta) -> dict[str, Any]:
    """Evaluate alerts with Betfair back prices (free). Returns {'ok', 'fired': [...]} or an error."""
    hours = window.total_seconds() / 3600
    summaries: list[dict[str, Any]] = []
    for team in sorted({a["team"] for a in group}):
        r = await _bf_summaries(sport, team, hours_ahead=hours, limit=3)
        if not r["ok"]:
            return r
        summaries.extend(r["markets"])
    if not summaries:
        return {"ok": False, "error": "no matching Betfair market"}
    events = [betfair.as_odds_api_event(sm) for sm in summaries]
    hits = []
    for a in group:
        hit = alerts.evaluate({**a, "event_id": ""}, events)
        if hit:
            hits.append((a, hit))
    return {"ok": True, "hits": hits}


# --------------------------------------------------------------------------
# Odds alerts
# --------------------------------------------------------------------------


def notify(title: str, message: str) -> list[str]:
    """macOS notification plus optional phone push via ntfy.sh (NTFY_TOPIC in .env)."""
    sent = []
    if sys.platform == "darwin":
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'
        try:
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
            sent.append("mac")
        except (OSError, subprocess.SubprocessError):
            pass
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        try:
            httpx.post(f"https://ntfy.sh/{topic}", content=message.encode(), headers={"Title": title}, timeout=10)
            sent.append("phone")
        except httpx.HTTPError:
            pass
    return sent


def _poll_interval_minutes(remaining: int | None, sports_watched: int) -> int:
    """Spread the remaining monthly credits evenly until the reset day."""
    floor = int(os.environ.get("ALERT_MIN_INTERVAL_MIN", 15))
    if remaining is None:
        remaining = int(os.environ.get("ODDS_MONTHLY_CREDITS", 500))
    reset_day = int(os.environ.get("ODDS_RESET_DAY", 1))
    today = _now().date()
    nxt = today.replace(day=min(reset_day, 28))
    if nxt <= today:
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=min(reset_day, 28))
    days_left = max(1, (nxt - today).days)
    spendable = max(0, remaining - _reserve("odds"))
    per_day = spendable / days_left / max(1, sports_watched)
    if per_day <= 0:
        return 10**6
    return max(floor, math.ceil(1440 / per_day))


async def run_alert_check(force: bool = False) -> dict[str, Any]:
    """Check active alerts. Spends credits only for sports that have a watched match starting within
    ALERT_WINDOW_HOURS (default 36) or in play, and no faster than the budget allows."""
    items = [a for a in alerts.load() if not a.get("triggered")]
    if not items:
        return {"checked": 0, "fired": [], "note": "No active alerts."}
    state = _load_state()
    last_poll = state.setdefault("last_poll", {})
    window = timedelta(hours=float(os.environ.get("ALERT_WINDOW_HOURS", 36)))
    by_sport: dict[str, list[dict[str, Any]]] = {}
    for a in items:
        by_sport.setdefault(a["sport_key"], []).append(a)
    interval = _poll_interval_minutes(state.get("odds_remaining"), len(by_sport))

    fired, skipped = [], []
    source = os.environ.get("ALERT_SOURCE", "auto").lower()
    floor = int(os.environ.get("ALERT_MIN_INTERVAL_MIN", 15))

    def _fire(a: dict[str, Any], hit: dict[str, Any]) -> None:
        a["triggered"] = True
        a["triggered_utc"] = _now().isoformat(timespec="seconds")
        a["hit"] = hit
        fired.append({"alert": a["id"], **hit})
        kind = a["kind"].replace("_", " ")
        notify(f"Odds alert: {hit['team']}", f"{hit['match']}: {hit['team']} {kind} "
               f"{a.get('target') or ''}. Best {hit['best_price']} at {hit['best_bookmaker']}.")

    for sport, group in by_sport.items():
        # 1) Betfair first when set up: free, so it can be checked every ALERT_MIN_INTERVAL_MIN.
        if source in ("auto", "betfair") and betfair.configured() and _bf_sport(sport):
            bf_key = f"betfair:{sport}"
            last = _parse_time(last_poll.get(bf_key))
            if not force and last and _now() - last < timedelta(minutes=floor):
                skipped.append(f"{sport}: Betfair checked {int((_now() - last).total_seconds() // 60)} min ago")
                continue
            bf = await _alerts_via_betfair(sport, group, window)
            last_poll[bf_key] = _now().isoformat(timespec="seconds")
            if bf["ok"]:
                for a, hit in bf["hits"]:
                    _fire(a, hit)
                continue
            skipped.append(f"{sport}: Betfair - {bf['error']}; trying The Odds API")
            if source == "betfair":
                continue
        if not _key("odds"):
            skipped.append(f"{sport}: no ODDS_API_KEY")
            continue
        ev = await _odds_events(sport)  # free
        if not ev["ok"]:
            skipped.append(f"{sport}: {ev['error']}")
            continue
        soon = [e for e in ev["data"] if any(models.same_team(t, a["team"]) for a in group
                for t in (e["home_team"], e["away_team"])) and (_parse_time(e["commence_time"]) or _now()) - _now() <= window]
        if not soon:
            skipped.append(f"{sport}: no watched match in the next {window.total_seconds() / 3600:.0f}h")
            continue
        last = _parse_time(last_poll.get(sport))
        if not force and last and _now() - last < timedelta(minutes=interval):
            skipped.append(f"{sport}: next check in {interval - int((_now() - last).total_seconds() // 60)} min (budget)")
            continue
        regions = os.environ.get("ALERT_REGIONS", "au")
        odds = await _odds_raw(sport, regions, "h2h", "", "", fresh=True)
        last_poll[sport] = _now().isoformat(timespec="seconds")
        if not odds["ok"]:
            skipped.append(f"{sport}: {odds['error']}")
            continue
        for a in group:
            hit = alerts.evaluate(a, odds["events"])
            if hit:
                _fire(a, hit)
    if fired:
        stored = alerts.load()
        for s in stored:
            for a in items:
                if s["id"] == a["id"] and a.get("triggered"):
                    s.update(a)
        alerts.save(stored)
    state["last_check_utc"] = _now().isoformat(timespec="seconds")
    _save_state(state)
    return {"checked": len(items), "poll_interval_min": interval, "fired": fired, "skipped": skipped}


@mcp.tool()
async def odds_alert_add(sport_key: str, team: str, kind: str = "price_at_or_below", target: float = 0.0,
                         event_id: str = "", note: str = "") -> dict[str, Any]:
    """Create an odds alert that the background watcher checks and pushes to your Mac (and phone if set up).
    With Betfair set up, alerts use Betfair back prices (free, checked every ~15 min); otherwise The Odds API.
    kind: price_at_or_below (e.g. target 1.5), price_at_or_above, becomes_favourite, stops_favourite.
    sport_key from odds_list_sports (e.g. cricket_ipl, soccer_epl). event_id optional (one match only)."""
    try:
        item = alerts.add(sport_key, team, kind, target or None, event_id, note)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if betfair.configured() and _bf_sport(sport_key):
        interval = int(os.environ.get("ALERT_MIN_INTERVAL_MIN", 15))
    else:
        interval = _poll_interval_minutes(_load_state().get("odds_remaining"),
                                          len({a["sport_key"] for a in alerts.load() if not a.get("triggered")}))
    return {"ok": True, "alert": item, "expected_check_every_min": interval,
            "note": "Checks only run while the watcher is installed (setup.command) and the Mac is awake and online."}


@mcp.tool()
async def odds_alert_list(include_triggered: bool = False) -> dict[str, Any]:
    """List odds alerts and the last background check time."""
    items = alerts.load()
    if not include_triggered:
        items = [a for a in items if not a.get("triggered")]
    return {"ok": True, "alerts": items, "last_check_utc": _load_state().get("last_check_utc")}


@mcp.tool()
async def odds_alert_remove(alert_id: str) -> dict[str, Any]:
    """Delete an odds alert by id."""
    return {"ok": alerts.remove(alert_id)}


@mcp.tool()
async def odds_alert_check_now() -> dict[str, Any]:
    """Check all alerts immediately, ignoring the budget pacing (costs 1 credit per watched sport)."""
    return await run_alert_check(force=True)


if __name__ == "__main__":
    mcp.run(transport="streamable-http" if HTTP_MODE else "stdio")

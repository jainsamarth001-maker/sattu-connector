"""Offline tests. The HTTP layer is replaced with canned provider responses routed by URL."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import alerts
import models
import server as s


def run(coro):
    return asyncio.run(coro)


def iso(hours_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")


def odds_event(home, away, books, hours=24, eid="evt1", sport="soccer_epl"):
    """books = {bookmaker_title: {outcome_name: price}}"""
    return {
        "id": eid, "sport_key": sport, "home_team": home, "away_team": away, "commence_time": iso(hours),
        "bookmakers": [{"key": t.lower(), "title": t, "markets": [{"key": "h2h", "outcomes": [
            {"name": n, "price": p} for n, p in prices.items()]}]} for t, prices in books.items()],
    }


EPL_EVENT = odds_event("Liverpool", "Manchester City", {
    "Sportsbet": {"Liverpool": 2.60, "Manchester City": 2.55, "Draw": 3.60},
    "TAB": {"Liverpool": 2.70, "Manchester City": 2.45, "Draw": 3.50},
})


def fd_match(mid, home, away, hg=None, ag=None, status="FINISHED", days=-7, season="2026-08-15"):
    return {"id": mid, "utcDate": iso(days * 24), "status": status, "matchday": 1, "season": {"startDate": season},
            "homeTeam": {"name": home}, "awayTeam": {"name": away},
            "score": {"fullTime": {"home": hg, "away": ag}}}


TEAMS = ["Liverpool FC", "Manchester City FC", "Arsenal FC", "Chelsea FC"]
FINISHED = []
_mid = 100
for d in range(3):
    for i, h in enumerate(TEAMS):
        for j, a in enumerate(TEAMS):
            if i != j:
                _mid += 1
                FINISHED.append(fd_match(_mid, h, a, (i + d) % 3, (j + 1) % 2, days=-60 + _mid - 100))


class Router:
    def __init__(self):
        self.routes = []
        self.calls = []

    def add(self, fragment, body, status=200, headers=None):
        self.routes.append((fragment, status, body, headers or {}))

    async def __call__(self, url, params=None, headers=None):
        self.calls.append((url, params))
        matches = [r for r in self.routes if r[0] in url]
        if not matches:
            return 404, {"message": "no route"}, {}
        frag, status, body, hdrs = max(matches, key=lambda r: len(r[0]))  # most specific route wins
        return status, body, hdrs


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    s._cache.clear()
    for q in s._quota.values():
        q.clear()
    for k, v in {"ODDS_API_KEY": "SECRET-ODDS", "API_FOOTBALL_KEY": "SECRET-AF", "FOOTBALL_DATA_KEY": "SECRET-FD",
                 "CRICKETDATA_API_KEY": "SECRET-CRIC", "LOCAL_TZ": "Australia/Brisbane"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ALERTS_FILE", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(s, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(s, "notify", lambda title, msg: ["test"])
    router = Router()
    monkeypatch.setattr(s, "_http_get", router)
    return router


# ---------------- models ----------------

def test_odds_maths():
    assert round(models.overround_pct([1.9, 1.9]), 2) == 5.26
    assert round(sum(models.no_vig_probs([1.8, 2.1, 3.4])), 9) == 1.0
    assert models.overround_pct([2.2, 2.2]) < 0
    assert round(models.edge_pct(0.5, 2.2), 1) == 10.0


@pytest.mark.parametrize("a,b", [("Man City", "Manchester City FC"), ("Liverpool", "Liverpool FC"),
                                 ("Spurs", "Tottenham Hotspur FC"), ("Brighton", "Brighton & Hove Albion FC"),
                                 ("CSK", "Chennai Super Kings"), ("Wolves", "Wolverhampton Wanderers FC")])
def test_team_matching(a, b):
    assert models.same_team(a, b)


def test_team_matching_rejects_different_clubs():
    assert not models.same_team("Manchester City", "Manchester United")
    assert not models.same_team("Liverpool", "Everton")


def test_poisson_model_sums_and_ranks():
    results = [{"home": "A", "away": "B", "home_goals": 3, "away_goals": 0},
               {"home": "B", "away": "A", "home_goals": 0, "away_goals": 2},
               {"home": "A", "away": "C", "home_goals": 2, "away_goals": 1},
               {"home": "C", "away": "B", "home_goals": 1, "away_goals": 1}]
    pred = models.predict_match(models.fit_strengths(results, shrink_games=1), "A", "B")
    p = pred["prob_pct"]
    assert abs(p["home"] + p["draw"] + p["away"] - 100) < 0.3
    assert p["home"] > p["away"]
    assert models.predict_match(models.fit_strengths(results), "A", "Zzz") is None


def test_analyse_market_favourite_and_edge():
    a = models.analyse_market({"X": {"b1": 1.5, "b2": 1.55}, "Y": {"b1": 2.6, "b2": 2.5}})
    assert a["favourite"] == "X"
    assert a["outcomes"]["X"]["best_bookmaker"] == "b2"
    assert "consensus_fair_odds" in a["outcomes"]["Y"]


# ---------------- odds tools ----------------

def test_compare_and_quota(env):
    env.add("/sports/soccer_epl/odds", [EPL_EVENT], headers={"x-requests-remaining": "480"})
    out = run(s.odds_compare("soccer_epl", team="Man City"))
    c = out["comparisons"][0]
    assert c["outcomes"]["Liverpool"]["best_bookmaker"] == "TAB"
    assert "AEST" in c["local_time"]
    assert s._quota["odds"]["remaining"] == 480
    assert s._load_state()["odds_remaining"] == 480


def test_cache_then_fresh(env):
    env.add("/sports/soccer_epl/odds", [EPL_EVENT])
    run(s.odds_get("soccer_epl"))
    assert run(s.odds_get("soccer_epl"))["cached"] is True
    run(s.odds_get("soccer_epl", fresh=True))
    assert len(env.calls) == 2


def test_cost_estimate():
    assert s._credit_cost("h2h", "au", "") == 1
    assert s._credit_cost("h2h,totals", "au,uk", "") == 4
    assert s._credit_cost("h2h", "au", ",".join(f"b{i}" for i in range(11))) == 2


def test_quota_guard(env):
    env.add("/odds", [EPL_EVENT])
    s._quota["odds"]["remaining"] = 21
    out = run(s.odds_get("soccer_epl", regions="au,uk"))
    assert not out["ok"] and "Quota guard" in out["error"] and env.calls == []


def test_network_block_message(env, monkeypatch):
    import httpx

    async def boom(*a, **k):
        raise httpx.ConnectError("reset")
    monkeypatch.setattr(s, "_http_get", boom)
    out = run(s.odds_get("soccer_epl"))
    assert not out["ok"] and "block" in out["error"]


def test_key_never_leaks(env):
    env.add("/odds", {"message": "invalid key SECRET-ODDS"}, status=401)
    assert "SECRET-ODDS" not in str(run(s.odds_get("soccer_epl")))


def test_missing_key(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY")
    assert "ODDS_API_KEY" in run(s.odds_get("soccer_epl"))["error"]


# ---------------- match preview ----------------

def test_match_preview_end_to_end(env):
    upcoming = fd_match(999, "Liverpool FC", "Manchester City FC", status="TIMED", days=1)
    env.add("/competitions/PL/matches", {"matches": [upcoming] + FINISHED})
    env.add("/competitions/PL/standings", {"standings": [{"type": "TOTAL", "table": [
        {"position": 1, "team": {"name": "Liverpool FC"}, "playedGames": 8, "won": 6, "draw": 1, "lost": 1, "goalsFor": 18,
         "goalsAgainst": 6, "goalDifference": 12, "points": 19, "form": "W,W,D,W,L"},
        {"position": 3, "team": {"name": "Manchester City FC"}, "playedGames": 8, "won": 5, "draw": 1, "lost": 2,
         "goalsFor": 15, "goalsAgainst": 8, "goalDifference": 7, "points": 16, "form": "W,L,W,W,D"}]}]})
    env.add("/matches/999/head2head", {"aggregates": {"numberOfMatches": 10, "totalGoals": 31,
            "homeTeam": {"wins": 4, "draws": 3, "losses": 3}, "awayTeam": {"wins": 3, "draws": 3, "losses": 4}},
            "matches": [fd_match(5, "Manchester City FC", "Liverpool FC", 1, 1, days=-200)]})
    env.add("/sports/soccer_epl/odds", [EPL_EVENT])

    out = run(s.match_preview("Liverpool", "Man City"))
    assert out["fixture"]["match_id"] == 999
    assert out["table"]["Liverpool"]["pos"] == 1
    assert out["head_to_head"]["matches"] == 10
    assert out["model"]["prob_pct"]["home"] > 0
    v = out["value_check"]
    assert set(v) == {"Liverpool", "Manchester City", "Draw"}
    assert "model_fair_odds" in v["Draw"] and "verdict" in v["Liverpool"]
    assert out["odds"]["favourite"] in v


def test_match_preview_without_odds_network(env):
    env.add("/competitions/PL/matches", {"matches": FINISHED})
    env.add("/competitions/PL/standings", {"standings": []})
    env.add("/sports/soccer_epl/odds", {"message": "x"}, status=500)
    out = run(s.match_preview("Liverpool", "Arsenal"))
    assert out["ok"] and out["model"] and any("Odds unavailable" in n for n in out["notes"])


# ---------------- cricket ----------------

def test_cricket_schedule_local_day(env):
    now_bne = datetime.now(timezone.utc) + timedelta(hours=10)
    today_noon_utc = now_bne.replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(hours=10)
    gmt = today_noon_utc.strftime("%Y-%m-%dT%H:%M:%S")
    env.add("api.cricapi.com/v1/matches", {"status": "success", "info": {"hitsToday": 2, "hitsLimit": 100}, "data": [
        {"id": "m1", "name": "India vs Australia, 1st ODI", "matchType": "odi", "status": "Match not started",
         "dateTimeGMT": gmt, "teams": ["India", "Australia"]},
        {"id": "m2", "name": "Old game", "dateTimeGMT": "2020-01-01T00:00:00", "teams": ["A", "B"]}]})
    env.add("/v4/sports", [{"key": "cricket_odi", "group": "Cricket", "title": "One Day Internationals", "active": True,
                            "has_outrights": False}])
    env.add("/sports/cricket_odi/events", [{"id": "c1", "home_team": "India", "away_team": "Australia",
                                             "commence_time": today_noon_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}])
    out = run(s.cricket_schedule("today"))
    assert [m["match_id"] for m in out["matches"]] == ["m1"]
    assert out["matches"][0]["odds_event_id"] == "c1"
    assert "12:00 PM" in out["matches"][0]["local_time"]


def test_cricket_failure_reason(env):
    env.add("currentMatches", {"status": "failure", "reason": "Hits today exceeded"})
    out = run(s.cricket_current_matches())
    assert not out["ok"] and "exceeded" in out["detail"]


# ---------------- alerts ----------------

def ipl(price_mi, price_csk, hours=5):
    return odds_event("Mumbai Indians", "Chennai Super Kings",
                      {"Sportsbet": {"Mumbai Indians": price_mi, "Chennai Super Kings": price_csk}}, hours, "ipl1", "cricket_ipl")


def test_alert_fires_once_when_price_drops(env):
    env.add("/sports/cricket_ipl/events", [ipl(1.6, 2.3)])
    env.add("/sports/cricket_ipl/odds", [ipl(1.5, 2.6)])
    assert run(s.odds_alert_add("cricket_ipl", "MI", "price_at_or_below", 1.5))["ok"]
    res = run(s.run_alert_check(force=True))
    assert res["fired"][0]["best_price"] == 1.5
    assert run(s.odds_alert_list())["alerts"] == []
    assert run(s.run_alert_check(force=True))["note"] == "No active alerts."


def test_alert_not_fired_above_target(env):
    env.add("/sports/cricket_ipl/events", [ipl(1.6, 2.3)])
    env.add("/sports/cricket_ipl/odds", [ipl(1.6, 2.3)])
    run(s.odds_alert_add("cricket_ipl", "Mumbai Indians", "price_at_or_below", 1.5))
    assert run(s.run_alert_check(force=True))["fired"] == []


def test_becomes_favourite(env):
    env.add("/sports/cricket_ipl/events", [ipl(2.0, 1.8)])
    env.add("/sports/cricket_ipl/odds", [ipl(1.7, 2.1)])
    run(s.odds_alert_add("cricket_ipl", "Mumbai Indians", "becomes_favourite"))
    assert run(s.run_alert_check(force=True))["fired"][0]["is_favourite"] is True


def test_alert_skips_far_matches_and_paces(env):
    env.add("/sports/cricket_ipl/events", [ipl(1.6, 2.3, hours=100)])
    run(s.odds_alert_add("cricket_ipl", "MI", "price_at_or_below", 1.5))
    res = run(s.run_alert_check())
    assert res["fired"] == [] and "no watched match" in res["skipped"][0]
    assert not any("/odds" in c[0] for c in env.calls)


def test_poll_interval_budget():
    assert s._poll_interval_minutes(10, 1) == 10**6  # under reserve -> stop
    assert s._poll_interval_minutes(500, 1) >= 15
    assert s._poll_interval_minutes(500, 3) > s._poll_interval_minutes(500, 1)


def test_alert_validation():
    assert not run(s.odds_alert_add("cricket_ipl", "MI", "price_at_or_below", 0.0))["ok"]
    assert not run(s.odds_alert_add("cricket_ipl", "MI", "nonsense", 1.5))["ok"]


def test_tools_registered_and_read_only():
    names = {t.name for t in run(s.mcp.list_tools())}
    for expected in ("match_preview", "cricket_schedule", "odds_alert_add", "odds_compare", "football_table"):
        assert expected in names
    assert not any("place" in n or "bet_" in n for n in names)

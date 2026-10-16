"""Offline tests for the read-only Betfair add-on and the in-play money maths."""

import asyncio
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

import alerts
import betfair
import models
import server as s


def run(coro):
    return asyncio.run(coro)


def iso(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


CATALOGUE = [
    {"marketId": "1.111", "marketName": "Match Odds", "marketStartTime": iso(5),
     "event": {"name": "India Women v Zimbabwe Women", "openDate": iso(5)},
     "competition": {"name": "Women's ODI"},
     "runners": [{"selectionId": 1, "runnerName": "India Women"}, {"selectionId": 2, "runnerName": "Zimbabwe Women"}]},
    {"marketId": "1.222", "marketName": "Match Odds", "marketStartTime": iso(30),
     "event": {"name": "Liverpool v Man City", "openDate": iso(30)}, "competition": {"name": "English Premier League"},
     "runners": [{"selectionId": 10, "runnerName": "Liverpool"}, {"selectionId": 11, "runnerName": "Man City"},
                 {"selectionId": 12, "runnerName": "The Draw"}]},
]


def book(mid, prices):
    """prices = {selectionId: (back, lay)}"""
    return {"marketId": mid, "status": "OPEN", "inplay": False, "runners": [
        {"selectionId": sid, "status": "ACTIVE", "lastPriceTraded": b,
         "ex": {"availableToBack": [{"price": b, "size": 100}], "availableToLay": [{"price": l, "size": 80}]}}
        for sid, (b, l) in prices.items()]}


COMPS = {"10932509": "English Premier League", "999": "Bangladesh Premier League", "228": "UEFA Champions League"}
CATALOGUE[1]["competition"]["id"] = "10932509"
EXTRA = [
    {"marketId": "1.333", "marketName": "Match Odds", "marketStartTime": iso(3),
     "event": {"name": "City Club v Bashundhara Kings"}, "competition": {"id": "999", "name": "Bangladesh Premier League"},
     "runners": [{"selectionId": 20, "runnerName": "City Club"}, {"selectionId": 21, "runnerName": "Bashundhara Kings"},
                 {"selectionId": 22, "runnerName": "The Draw"}]},
    {"marketId": "1.444", "marketName": "Match Odds", "marketStartTime": iso(-6),
     "event": {"name": "Cavalier v Portmore Utd"}, "competition": {"id": "998", "name": "Jamaican Premier League"},
     "runners": [{"selectionId": 30, "runnerName": "Cavalier"}, {"selectionId": 31, "runnerName": "Portmore Utd"},
                 {"selectionId": 32, "runnerName": "The Draw"}]},
]

BOOKS = {"1.111": book("1.111", {1: (1.10, 1.11), 2: (9.8, 10.5)}),
         "1.222": book("1.222", {10: (2.62, 2.66), 11: (2.70, 2.74), 12: (3.70, 3.80)}),
         "1.333": book("1.333", {20: (4.4, 5.9), 21: (1.59, 1.78), 22: (3.85, 5.6)}),
         "1.444": {**book("1.444", {30: (1.04, 980), 31: (1.04, 980), 32: (1.01, 3.5)}), "status": "SUSPENDED"}}


class FakeBetfair:
    def __init__(self):
        self.calls = []
        self.login_body = {"token": "SESSION-TOKEN-123", "status": "SUCCESS", "error": ""}
        self.expire_next = False
        self.fail_api = False

    async def __call__(self, url, *, data=None, json_body=None, headers=None):
        self.calls.append((url, data, json_body, headers))
        if "login" in url:
            return 200, self.login_body
        if self.fail_api:
            return 503, {"raw": "down"}
        if self.expire_next:
            self.expire_next = False
            return 200, {"error": {"data": {"APINGException": {"errorCode": "INVALID_SESSION_INFORMATION"}}}}
        method = json_body["method"].split("/")[-1]
        if method == "listEventTypes":
            return 200, {"result": [{"eventType": {"id": "1", "name": "Soccer"}}, {"eventType": {"id": "4", "name": "Cricket"}}]}
        if method == "listCompetitions":
            return 200, {"result": [{"competition": {"id": cid, "name": n}} for cid, n in COMPS.items()]}
        if method == "listMarketCatalogue":
            f = json_body["params"]["filter"]
            want = f["eventTypeIds"][0]
            res = [c for c in CATALOGUE + EXTRA if (want == "4") == (c.get("sport") == "cricket" or "Women" in c["event"]["name"])]
            if "competitionIds" in f:
                res = [c for c in res if c["competition"].get("id") in f["competitionIds"]]
            return 200, {"result": res}
        if method == "listMarketBook":
            return 200, {"result": [BOOKS[m] for m in json_body["params"]["marketIds"]]}
        return 200, {"error": {"message": "unknown"}}

    def logins(self):
        return [c for c in self.calls if "login" in c[0]]


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    s._cache.clear()
    for q in s._quota.values():
        q.clear()
    for k, v in {"BETFAIR_APP_KEY": "APPKEY-XYZ", "BETFAIR_USERNAME": "samuser", "BETFAIR_PASSWORD": "pa$$w0rd!",
                 "LOCAL_TZ": "Australia/Brisbane", "ODDS_API_KEY": "SECRET-ODDS"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BETFAIR_SESSION_FILE", str(tmp_path / "bf.json"))
    monkeypatch.setenv("ALERTS_FILE", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(s, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(s, "notify", lambda title, msg: ["test"])
    fake = FakeBetfair()
    monkeypatch.setattr(betfair, "_http_post", fake)
    yield fake


def test_write_operations_refused_without_network(env):
    for op in ("placeOrders", "cancelOrders", "replaceOrders", "updateOrders"):
        r = run(betfair.call(op, {}))
        assert not r["ok"] and "never places" in r["error"]
    assert env.calls == []


def test_login_reused_and_session_file_private(env, tmp_path):
    run(s.betfair_status())
    run(s.betfair_status())
    assert len(env.logins()) == 1
    assert stat.S_IMODE(os.stat(tmp_path / "bf.json").st_mode) == 0o600
    login_call = env.logins()[0]
    assert login_call[3]["X-Application"] == "APPKEY-XYZ" and login_call[1]["username"] == "samuser"


def test_failed_login_backs_off_to_protect_account(env):
    env.login_body = {"token": "", "status": "FAIL", "error": "INVALID_USERNAME_OR_PASSWORD"}
    r1 = run(s.betfair_status())
    r2 = run(s.betfair_status())
    assert not r1["ok"] and "INVALID_USERNAME_OR_PASSWORD" in r1["error"]
    assert "Not retrying" in r2["error"]
    assert len(env.logins()) == 1


def test_expired_session_relogs_once(env):
    run(s.betfair_status())
    s._cache.clear()
    env.expire_next = True
    r = run(s.betfair_match_odds("India Women", sport="cricket"))
    assert r["ok"]
    assert len(env.logins()) == 2


def test_match_odds_shape(env):
    r = run(s.betfair_match_odds("Liverpool", "Manchester City", sport="soccer"))
    m = r["markets"][0]
    assert m["favourite"] == "Liverpool"
    names = [x["name"] for x in m["runners"]]
    assert "Draw" in names
    liv = m["runners"][0]
    assert liv["back_price"] == 2.62 and liv["lay_price"] == 2.66 and liv["mid_price"] == 2.64
    assert liv["implied_chance_pct"] == round(100 / 2.64, 1)
    assert m["local_time"] and "AEST" in m["local_time"]


def test_unknown_team_gives_clear_message(env):
    r = run(s.betfair_match_odds("Atlantis FC", sport="soccer"))
    assert not r["ok"] and "No Betfair" in r["error"]


def test_secrets_never_leak_in_errors(env):
    env.fail_api = True
    r = run(s.betfair_match_odds("India Women", sport="cricket"))
    text = str(r)
    for secret in ("APPKEY-XYZ", "pa$$w0rd!", "SESSION-TOKEN-123"):
        assert secret not in text


def test_not_configured(monkeypatch, env):
    monkeypatch.delenv("BETFAIR_PASSWORD")
    r = run(s.betfair_status())
    assert not r["ok"] and r["configured"] is False
    assert env.calls == []


def test_alert_fires_from_betfair_without_spending_odds_credits(env, monkeypatch):
    odds_calls = []

    async def no_odds(*a, **k):
        odds_calls.append(a)
        return 200, [], {}

    monkeypatch.setattr(s, "_http_get", no_odds)
    alerts.add("cricket_odi", "India Women", "price_at_or_below", 1.15)
    r = run(s.run_alert_check())
    assert r["fired"] and r["fired"][0]["best_bookmaker"] == "Betfair Exchange (back)"
    assert r["fired"][0]["best_price"] == 1.10
    assert odds_calls == []


def test_alert_falls_back_to_odds_api_when_betfair_fails(env, monkeypatch):
    env.fail_api = True
    seen = []

    async def odds(url, params=None, headers=None):
        seen.append(url)
        return 200, [], {"x-requests-remaining": "400"}

    monkeypatch.setattr(s, "_http_get", odds)
    alerts.add("soccer_epl", "Liverpool", "becomes_favourite", None)
    r = run(s.run_alert_check(force=True))
    assert any("Betfair" in x for x in r["skipped"])
    assert any("the-odds-api" in u for u in seen)


def test_hedge_equalises_profit():
    h = models.hedge(20, 2.5, 1.8, commission_pct=5)
    assert h["profit_if_team_wins"] == h["profit_if_team_loses"] == 7.14
    assert h["lay_stake"] == 28.57
    h2 = models.hedge(20, 2.5, 1.8, commission_pct=5, back_on_betfair=True)
    assert h2["locked_in"] > 0


def test_cash_out_verdicts():
    fair = models.cash_out_check(20, 2.5, 31, current_odds=1.6)
    cheap = models.cash_out_check(20, 2.5, 25, current_odds=1.6)
    assert fair["verdict"].startswith("Fair") or "Small" in fair["verdict"]
    assert cheap["verdict"].startswith("Expensive")
    with pytest.raises(ValueError):
        models.cash_out_check(20, 2.5, 25)


def test_no_betting_tools_exposed():
    names = [t.name for t in run(s.mcp.list_tools())]
    assert {"betfair_status", "betfair_find_markets", "betfair_match_odds", "hedge_calculator", "cash_out_check"} <= set(names)
    for bad in ("place", "cancel", "order", "bet_on", "deposit", "withdraw"):
        assert not any(bad in n for n in names)


def test_featured_soccer_only_by_default(env):
    r = run(s.betfair_find_markets("soccer"))
    events = [m["event"] for m in r["markets"]]
    assert events == ["Liverpool v Man City"]
    r_all = run(s.betfair_find_markets("soccer", featured_only=False))
    assert "City Club v Bashundhara Kings" in [m["event"] for m in r_all["markets"]]


def test_finished_suspended_market_hidden(env):
    r = run(s.betfair_find_markets("soccer", featured_only=False))
    assert "Cavalier v Portmore Utd" not in [m["event"] for m in r["markets"]]


def test_thin_market_uses_last_traded_and_flags_it():
    price, thin = betfair.fair_price(1.04, 980, 3.85)
    assert price == 3.85 and thin
    price, thin = betfair.fair_price(2.62, 2.66, 2.6)
    assert price == 2.64 and not thin


def test_featured_names():
    assert betfair.is_featured_soccer("English Premier League")
    assert betfair.is_featured_soccer("UEFA Europa Conference League")
    assert not betfair.is_featured_soccer("Bangladesh Premier League")
    assert not betfair.is_featured_soccer("UEFA Women's Champions League")

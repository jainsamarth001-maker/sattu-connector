"""Pure calculations: odds maths, team-name matching and a simple football goals model.

Nothing here touches the network, so all of it is unit-tested offline.
"""

from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

# --------------------------------------------------------------------------
# Odds maths
# --------------------------------------------------------------------------


def implied_prob(decimal_odds: float) -> float:
    return 1.0 / decimal_odds


def overround_pct(prices: list[float]) -> float:
    """Bookmaker margin in percent for a complete set of outcomes. Negative means arbitrage."""
    return (sum(1.0 / p for p in prices) - 1.0) * 100.0


def no_vig_probs(prices: list[float]) -> list[float]:
    total = sum(1.0 / p for p in prices)
    return [(1.0 / p) / total for p in prices]


def edge_pct(prob: float, decimal_odds: float) -> float:
    """Expected value per unit staked, in percent. Positive means the price beats the probability."""
    return (prob * decimal_odds - 1.0) * 100.0


def analyse_market(labels: dict[str, dict[str, float]]) -> dict[str, Any]:
    """labels = {outcome: {bookmaker: decimal_price}} for ONE market of ONE event."""
    outcomes: dict[str, Any] = {}
    for label, books in labels.items():
        best_book, best_price = max(books.items(), key=lambda kv: kv[1])
        outcomes[label] = {
            "best_bookmaker": best_book,
            "best_price": best_price,
            "average_price": round(sum(books.values()) / len(books), 3),
            "bookmakers_quoting": len(books),
        }

    all_books = {b for books in labels.values() for b in books}
    complete = [b for b in all_books if all(b in books for books in labels.values())]
    margins = {b: round(overround_pct([labels[l][b] for l in labels]), 2) for b in sorted(complete)}

    result: dict[str, Any] = {"outcomes": outcomes, "bookmaker_margin_pct": margins}
    if len(labels) >= 2:
        best_prices = [o["best_price"] for o in outcomes.values()]
        combined = overround_pct(best_prices)
        result["best_prices_combined_margin_pct"] = round(combined, 2)
        result["arbitrage"] = combined < 0
        avg_prices = [o["average_price"] for o in outcomes.values()]
        for o, p in zip(outcomes.values(), no_vig_probs(avg_prices)):
            o["consensus_fair_prob_pct"] = round(p * 100, 1)
            o["consensus_fair_odds"] = round(1 / p, 3)
            o["best_price_edge_vs_consensus_pct"] = round(edge_pct(p, o["best_price"]), 2)
        fav = min(outcomes.items(), key=lambda kv: kv[1]["average_price"])
        result["favourite"] = fav[0]
    return result


# --------------------------------------------------------------------------
# Team-name matching across providers ("Man City" vs "Manchester City FC")
# --------------------------------------------------------------------------

ALIASES = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "nottm forest": "nottingham forest",
    "nott m forest": "nottingham forest",
    "brighton": "brighton and hove albion",
    "brighton hove albion": "brighton and hove albion",
    "west ham": "west ham united",
    "newcastle": "newcastle united",
    "leeds": "leeds united",
    "sheffield utd": "sheffield united",
    "psg": "paris saint germain",
    "inter": "inter milan",
    "internazionale": "inter milan",
    "fc internazionale milano": "inter milan",
    "bayern": "bayern munich",
    "fc bayern munchen": "bayern munich",
    "bayern munchen": "bayern munich",
    "atletico": "atletico madrid",
    "club atletico de madrid": "atletico madrid",
    "rcb": "royal challengers bengaluru",
    "royal challengers bangalore": "royal challengers bengaluru",
    "csk": "chennai super kings",
    "mi": "mumbai indians",
    "kkr": "kolkata knight riders",
    "srh": "sunrisers hyderabad",
    "dc": "delhi capitals",
    "pbks": "punjab kings",
    "rr": "rajasthan royals",
    "lsg": "lucknow super giants",
    "gt": "gujarat titans",
}

_NOISE = re.compile(r"\b(fc|afc|cf|sc|ac|ssc|club|de|the|w)\b")


def normalise_team(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ").replace("'", " ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text in ALIASES:
        return ALIASES[text]
    stripped = re.sub(r"\s+", " ", _NOISE.sub(" ", text)).strip()
    return ALIASES.get(stripped, stripped)


def team_similarity(a: str, b: str) -> float:
    na, nb = normalise_team(a), normalise_team(b)
    if na == nb:
        return 1.0
    if na and nb and (na in nb or nb in na):
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def same_team(a: str, b: str, threshold: float = 0.84) -> bool:
    return team_similarity(a, b) >= threshold


def find_team_index(target: str, candidates: list[str]) -> int | None:
    scored = [(team_similarity(target, c), i) for i, c in enumerate(candidates)]
    if not scored:
        return None
    best, idx = max(scored)
    return idx if best >= 0.84 else None


# --------------------------------------------------------------------------
# Football goals model (team strengths + Poisson), deliberately simple
# --------------------------------------------------------------------------


def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def fit_strengths(results: list[dict[str, Any]], shrink_games: float = 6.0) -> dict[str, Any]:
    """results: [{home, away, home_goals, away_goals}] for finished matches.

    Attack/defence ratings relative to league average, shrunk toward 1.0 when a
    team has played few games (shrink_games acts like that many average games).
    """
    if not results:
        return {"teams": {}, "home_avg": 1.5, "away_avg": 1.2, "matches": 0}
    n = len(results)
    home_avg = max(0.3, sum(r["home_goals"] for r in results) / n)
    away_avg = max(0.3, sum(r["away_goals"] for r in results) / n)

    agg: dict[str, dict[str, float]] = {}
    for r in results:
        h = agg.setdefault(r["home"], {"hs": 0, "hc": 0, "hn": 0, "as": 0, "ac": 0, "an": 0})
        a = agg.setdefault(r["away"], {"hs": 0, "hc": 0, "hn": 0, "as": 0, "ac": 0, "an": 0})
        h["hs"] += r["home_goals"]; h["hc"] += r["away_goals"]; h["hn"] += 1
        a["as"] += r["away_goals"]; a["ac"] += r["home_goals"]; a["an"] += 1

    def shrunk(scored: float, games: float, avg: float) -> float:
        return (scored + shrink_games * avg) / ((games + shrink_games) * avg)

    teams = {}
    for team, s in agg.items():
        teams[team] = {
            "attack_home": shrunk(s["hs"], s["hn"], home_avg),
            "defence_home": shrunk(s["hc"], s["hn"], away_avg),
            "attack_away": shrunk(s["as"], s["an"], away_avg),
            "defence_away": shrunk(s["ac"], s["an"], home_avg),
            "games": s["hn"] + s["an"],
        }
    return {"teams": teams, "home_avg": home_avg, "away_avg": away_avg, "matches": n}


def predict_match(model: dict[str, Any], home: str, away: str, max_goals: int = 10) -> dict[str, Any] | None:
    names = list(model["teams"])
    hi, ai = find_team_index(home, names), find_team_index(away, names)
    if hi is None or ai is None:
        return None
    h, a = model["teams"][names[hi]], model["teams"][names[ai]]
    lam_h = model["home_avg"] * h["attack_home"] * a["defence_away"]
    lam_a = model["away_avg"] * a["attack_away"] * h["defence_home"]

    p_home = p_draw = p_away = p_over25 = p_btts = 0.0
    top: list[tuple[float, str]] = []
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = _poisson(i, lam_h) * _poisson(j, lam_a)
            if i > j:
                p_home += p
            elif i == j:
                p_draw += p
            else:
                p_away += p
            if i + j > 2:
                p_over25 += p
            if i > 0 and j > 0:
                p_btts += p
            top.append((p, f"{i}-{j}"))
    total = p_home + p_draw + p_away
    top.sort(reverse=True)
    return {
        "expected_goals": {"home": round(lam_h, 2), "away": round(lam_a, 2)},
        "prob_pct": {
            "home": round(100 * p_home / total, 1),
            "draw": round(100 * p_draw / total, 1),
            "away": round(100 * p_away / total, 1),
            "over_2_5": round(100 * p_over25 / total, 1),
            "btts": round(100 * p_btts / total, 1),
        },
        "fair_odds": {
            "home": round(total / p_home, 2),
            "draw": round(total / p_draw, 2),
            "away": round(total / p_away, 2),
        },
        "most_likely_scores": [{"score": s, "prob_pct": round(100 * p, 1)} for p, s in top[:3]],
        "games_used": {"home": h["games"], "away": a["games"], "league_matches": model["matches"]},
    }


def form_string(results: list[dict[str, Any]], team: str, last_n: int = 5) -> dict[str, Any]:
    """W/D/L form for a team from a chronological list of finished matches."""
    played = [r for r in results if same_team(r["home"], team) or same_team(r["away"], team)]
    played = played[-last_n:]
    letters, gf, ga = [], 0, 0
    for r in played:
        home = same_team(r["home"], team)
        f, a = (r["home_goals"], r["away_goals"]) if home else (r["away_goals"], r["home_goals"])
        gf += f
        ga += a
        letters.append("W" if f > a else "D" if f == a else "L")
    return {
        "last": "".join(letters),
        "goals_for": gf,
        "goals_against": ga,
        "matches": [
            f"{r.get('date', '')[:10]} {r['home']} {r['home_goals']}-{r['away_goals']} {r['away']}" for r in played
        ],
    }


# --------------------------------------------------------------------------
# In-play money maths: hedging on Betfair, judging a cash-out offer
# --------------------------------------------------------------------------


def hedge(back_stake: float, back_odds: float, lay_odds: float, commission_pct: float = 5.0,
          back_on_betfair: bool = False) -> dict[str, Any]:
    """Lay stake on Betfair that gives the same profit whichever way the match goes.

    Default: your back bet is with a bookmaker (Sportsbet, bet365...) and only the Betfair lay
    pays commission. back_on_betfair=True: both bets are on Betfair, commission on the net win.
    """
    if back_stake <= 0 or back_odds <= 1 or lay_odds <= 1:
        raise ValueError("stake must be above 0 and odds above 1.0")
    c = max(0.0, commission_pct) / 100
    if back_on_betfair:
        lay = back_stake * back_odds / lay_odds
        win = back_stake * (back_odds - 1) - lay * (lay_odds - 1)
        lose = -back_stake + lay
        win, lose = (win * (1 - c) if win > 0 else win), (lose * (1 - c) if lose > 0 else lose)
    else:
        lay = back_stake * back_odds / (lay_odds - c)
        win = back_stake * (back_odds - 1) - lay * (lay_odds - 1)
        lose = -back_stake + lay * (1 - c)
    return {
        "lay_stake": round(lay, 2),
        "lay_liability": round(lay * (lay_odds - 1), 2),
        "profit_if_team_wins": round(win, 2),
        "profit_if_team_loses": round(lose, 2),
        "locked_in": round(min(win, lose), 2),
        "commission_pct": commission_pct,
    }


def cash_out_check(stake: float, odds_taken: float, cash_out_offer: float, current_odds: float = 0.0,
                   my_win_chance_pct: float = 0.0) -> dict[str, Any]:
    """Compare a bookmaker's cash-out offer with what the bet is worth now.

    Worth now = chance the team wins x full payout. The chance comes from my_win_chance_pct if given,
    otherwise from the current odds (1 / current_odds, slightly pessimistic because of the margin).
    """
    if stake <= 0 or odds_taken <= 1 or cash_out_offer < 0:
        raise ValueError("stake must be above 0, odds above 1.0 and the offer 0 or more")
    if my_win_chance_pct > 0:
        p, basis = my_win_chance_pct / 100, "your/our estimated win chance"
    elif current_odds > 1:
        p, basis = 1 / current_odds, f"current odds {current_odds}"
    else:
        raise ValueError("give current_odds (from your app or Betfair) or my_win_chance_pct")
    payout = stake * odds_taken
    worth = p * payout
    ratio = cash_out_offer / worth if worth else 0
    if ratio >= 0.97:
        verdict = "Fair offer. Taking it costs you almost nothing on average; take it if you want certainty."
    elif ratio >= 0.90:
        verdict = f"Small hidden fee of about {round((1 - ratio) * 100)}%. Fine if you need to lock in; otherwise holding is worth a bit more on average."
    else:
        verdict = f"Expensive: the offer is {round((1 - ratio) * 100)}% below what the bet is worth. Holding, or a Betfair lay hedge, returns more on average."
    return {
        "full_payout_if_wins": round(payout, 2),
        "win_chance_used_pct": round(p * 100, 1),
        "chance_basis": basis,
        "bet_worth_now": round(worth, 2),
        "cash_out_offer": cash_out_offer,
        "offer_vs_worth_pct": round(ratio * 100, 1),
        "profit_if_cash_out": round(cash_out_offer - stake, 2),
        "verdict": verdict,
        "reminder": "Average value is not certainty: holding can still lose the whole stake.",
    }

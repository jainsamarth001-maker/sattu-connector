"""Odds alerts: stored in alerts.json, checked by watcher.py (runs in the background on your Mac).

An alert fires once, then is marked as triggered. Kinds:
  price_at_or_below  team's BEST available price <= target   ("tell me when it drops to 1.5")
  price_at_or_above  team's BEST available price >= target
  becomes_favourite  team has the shortest consensus price in the match
  stops_favourite    team is no longer the shortest consensus price
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import models

KINDS = ("price_at_or_below", "price_at_or_above", "becomes_favourite", "stops_favourite")


def alerts_path() -> Path:
    return Path(os.environ.get("ALERTS_FILE", Path(__file__).with_name("alerts.json")))


def load() -> list[dict[str, Any]]:
    path = alerts_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def save(items: list[dict[str, Any]]) -> None:
    path = alerts_path()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".alerts-")
    with os.fdopen(fd, "w") as fh:
        json.dump(items, fh, indent=2)
    os.replace(tmp, path)


def add(sport_key: str, team: str, kind: str, target: float | None, event_id: str = "", note: str = "") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind.startswith("price") and (target is None or target <= 1.0):
        raise ValueError("price alerts need a decimal target greater than 1.0, for example 1.5")
    item = {
        "id": uuid.uuid4().hex[:8],
        "sport_key": sport_key,
        "team": team,
        "kind": kind,
        "target": target,
        "event_id": event_id,
        "note": note,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "triggered": False,
    }
    items = load()
    items.append(item)
    save(items)
    return item


def remove(alert_id: str) -> bool:
    items = load()
    kept = [a for a in items if a["id"] != alert_id]
    save(kept)
    return len(kept) != len(items)


def evaluate(alert: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """events: raw The Odds API events (h2h market). Returns a hit description or None."""
    for event in events:
        if alert.get("event_id") and event.get("id") != alert["event_id"]:
            continue
        teams = [event.get("home_team", ""), event.get("away_team", "")]
        if not any(models.same_team(t, alert["team"]) for t in teams):
            continue

        labels: dict[str, dict[str, float]] = {}
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for o in market.get("outcomes", []):
                    labels.setdefault(o["name"], {})[book["title"]] = o["price"]
        if not labels:
            continue
        team_label = next((l for l in labels if models.same_team(l, alert["team"])), None)
        if team_label is None:
            continue

        analysis = models.analyse_market(labels)
        mine = analysis["outcomes"][team_label]
        is_fav = analysis.get("favourite") == team_label
        kind, target = alert["kind"], alert.get("target")
        hit = (
            (kind == "price_at_or_below" and mine["best_price"] <= target)
            or (kind == "price_at_or_above" and mine["best_price"] >= target)
            or (kind == "becomes_favourite" and is_fav)
            or (kind == "stops_favourite" and not is_fav)
        )
        if hit:
            return {
                "match": f"{event['home_team']} v {event['away_team']}",
                "team": team_label,
                "best_price": mine["best_price"],
                "best_bookmaker": mine["best_bookmaker"],
                "average_price": mine["average_price"],
                "is_favourite": is_fav,
                "commence_time": event.get("commence_time"),
            }
    return None

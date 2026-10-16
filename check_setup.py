"""Live connectivity check for each provider. Uses at most 1 CricketData hit and no odds credits."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import server as s  # noqa: E402

CHECKS = [
    ("The Odds API (odds)", "odds", s._odds_sports),
    ("football-data.org (fixtures, table, H2H)", "fdorg", lambda: s._fd("/competitions/PL/standings", None, 60)),
    ("API-Football (live + player stats)", "football", lambda: s._fetch(
        "football", f"{s.APIFOOTBALL_BASE}/status", None, {"x-apisports-key": s._key("football") or ""}, 60, cost=0)),
    ("CricketData (cricket schedule + scores)", "cricket", lambda: s._cricket("currentMatches", {"offset": 0}, 60)),
    ("Betfair Exchange (read-only prices)", "betfair", s.betfair_status),
]


async def main() -> int:
    print(f"\nLocal time zone: {s._tz()}\n")
    ok_count = 0
    for label, provider, fn in CHECKS:
        if (provider == "betfair" and not s.betfair.configured()) or (provider != "betfair" and not s._key(provider)):
            print(f"  [ ] {label}: no key in .env yet")
            continue
        r = await fn()
        if r.get("ok"):
            ok_count += 1
            print(f"  [OK] {label}")
        else:
            print(f"  [X] {label}: {r.get('error')} {r.get('detail', '')}")
    print("\nIf odds fail with 'Could not reach' while the others work, your network blocks betting sites.")
    print("Try home Wi-Fi with any work VPN switched off, or use a personal computer.\n")
    return ok_count


if __name__ == "__main__":
    asyncio.run(main())

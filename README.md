# Sports betting research for Claude (read-only)

Ask Claude things like:

- "Liverpool v Man City: who should I back, what's the head-to-head, are the odds fair?" -> `match_preview`
- "What cricket is on today and at what time?" -> `cricket_schedule`
- "Tell me when Mumbai Indians drop to 1.5" / "...when India become favourites" -> `odds_alert_add`

It cannot place bets. There is no betting code in this project.

## What connects to what

| Need | Source | Free limit | Key |
|---|---|---|---|
| Bookmaker odds (Sportsbet, TAB, Pinnacle, Betfair...) | The Odds API | 500 credits/month | ODDS_API_KEY |
| Fixtures, league table, head-to-head, results for the model | football-data.org | 10 requests/min | FOOTBALL_DATA_KEY |
| Live football match stats, player stats | API-Football | 100/day, older seasons only on free plan | API_FOOTBALL_KEY |
| Cricket schedule, live scores, player careers | CricketData.org | 100 hits/day | CRICKETDATA_API_KEY |
| Cricket analytics: win probability, venue, matchups, form | Cricket for Noobs (hosted MCP) | free, no key | none |
| Betfair Exchange back/lay prices, free alerts (optional) | Betfair API, delayed key (read-only) | free; prices 1-180 s old | BETFAIR_APP_KEY, _USERNAME, _PASSWORD |

## Betfair (optional, recommended)

Gives exchange prices for cricket and soccer, including in-play, and makes alerts free: with Betfair set up,
alerts check Betfair every 15 minutes and do not use Odds API credits.

1. Log in to Betfair, then follow Betfair Australia's guide to create your app keys:
   https://betfair-datascientists.github.io/api/apiappkey/ . Copy the **Version 1.0-Delay** key (free).
   Do not activate the live key; Betfair asks read-only users to use the delayed key.
2. Run `setup.command` again. It asks for the key, your username and your password (typed hidden).
3. In Claude: "Betfair status" should say OK.

Safety: the Betfair code can only call five read operations (list sports, competitions, events,
markets, prices). Placing, cancelling or changing bets is refused in code and tested. Your password stays
in `.env` on your Mac (readable only by you). If Betfair two-step login is on, this simple login will not
work; tell Claude and certificate login can be added. A wrong password is not retried for 30 minutes, so
the account cannot get locked by the watcher.

New tools: `betfair_status`, `betfair_find_markets`, `betfair_match_odds`, `hedge_calculator`
(lock in a profit with a lay bet), `cash_out_check` (is a cash-out offer fair?).

## Setup (macOS, about 5 minutes)

1. Get the free keys (links are in `.env.example`). Each signup emails you a key.
2. Double-click `setup.command`. If macOS warns, right-click it, choose Open, then Open.
   It installs Python via uv, runs the tests, asks for your keys, connects Claude Desktop,
   starts the alert watcher and checks every provider live.
3. Quit Claude Desktop with Cmd+Q and reopen it.
4. Optional, recommended for cricket: Claude Settings, Connectors, Add custom connector,
   URL `https://cricketfornoobs.com/mcp`. This one also works in Claude on your phone.

Check anytime: run `.venv/bin/python check_setup.py` in this folder.

## How the free limits are protected

- Odds cost markets x regions credits per call (1 for match-winner in one region). Listing sports and
  events is free. Results are cached (odds 2 min, fixtures 10 min, stats 1 hour).
- A quota guard stops before credits run out (keeps 20 in reserve).
- Alerts only spend credits when a watched match starts within 36 hours, and space checks so the
  month's credits last. With 500 credits and one sport, expect a check roughly every 1.5 hours.
  For near-live alerts, the $30/month Odds API plan (20,000 credits) allows a check every few minutes.

## Honest limits

- Odds refresh at the source every 40-60 seconds; alerts on the free plan are far slower than that.
- Alerts only run while your Mac is awake and online.
- Some networks (work laptops, VPNs) block betting and odds sites. `check_setup.py` shows this as
  "Could not reach". The stats sources will still work.
- The goals model is deliberately simple. Bookmaker consensus prices are usually the better estimate;
  a "value" flag means the best available price beats the consensus fair price, not that the bet will win.

## Files

- `server.py`: the MCP server (tools)  - `models.py`: odds maths, team-name matching, goals model
- `alerts.py`, `watcher.py`: alerts and the background checker  - `check_setup.py`: live provider check
- `betfair.py`: read-only Betfair client  - `test_server.py`, `test_betfair.py`: 41 offline tests (`.venv/bin/python -m pytest -q`)

To remove the watcher: `launchctl unload ~/Library/LaunchAgents/com.sportsodds.watcher.plist`

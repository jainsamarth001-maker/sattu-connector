#!/bin/bash
# One-time setup for macOS. Double-click this file (or run: bash setup.command).
# It installs the Python tools it needs, asks for your free API keys, connects the
# server to Claude Desktop and starts the background odds-alert watcher.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
echo "== Sports odds connector setup =="
echo "Folder: $HERE"

# 1. uv (manages Python for us; no admin rights needed)
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then
  echo "Installing uv (Python manager) into ~/.local/bin ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi

# 2. Python environment + dependencies
"$UV" venv --python 3.12 --allow-existing .venv >/dev/null
"$UV" pip install --python .venv/bin/python -q -r requirements.txt pytest
PY="$HERE/.venv/bin/python"

# 3. Offline tests
echo "Running offline tests..."
"$PY" -m pytest -q

# 4. API keys (written safely by Python, so special characters in passwords are fine)
[ -f .env ] || cp .env.example .env
set_env () {
  "$PY" - "$1" "$2" <<'PYEOF'
import sys
from pathlib import Path
var, value = sys.argv[1], sys.argv[2]
quoted = f"'{value}'" if "'" not in value else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
env = Path(".env")
lines = env.read_text().splitlines() if env.exists() else []
out, done = [], False
for line in lines:
    if line.startswith(var + "="):
        out.append(f"{var}={quoted}"); done = True
    else:
        out.append(line)
if not done:
    out.append(f"{var}={quoted}")
env.write_text("\n".join(out) + "\n")
PYEOF
}
ask_key () {
  local var="$1" label="$2" url="$3" secret="${4:-}"
  local current
  current="$(grep -E "^$var=" .env | cut -d= -f2- || true)"
  if [ -z "$current" ] || [ "$current" = "''" ]; then
    echo ""
    echo "$label  ($url)"
    if [ -n "$secret" ]; then
      read -r -s -p "Type $var (hidden) and press Enter (or just Enter to skip): " value; echo ""
    else
      read -r -p "Paste $var and press Enter (or just Enter to skip): " value
    fi
    if [ -n "$value" ]; then set_env "$var" "$value"; fi
  fi
}
ask_key ODDS_API_KEY "The Odds API - bookmaker odds" "https://the-odds-api.com"
ask_key FOOTBALL_DATA_KEY "football-data.org - fixtures, table, head-to-head" "https://www.football-data.org/client/register"
ask_key API_FOOTBALL_KEY "API-Football - live match and player stats" "https://dashboard.api-football.com/register"
ask_key CRICKETDATA_API_KEY "CricketData.org - cricket schedule and scores" "https://cricketdata.org/signup.aspx"
echo ""
echo "Betfair (optional, free, read-only): exchange prices and free 15-minute alerts."
echo "Use the 'Version 1.0-Delay' app key. How to get it: https://betfair-datascientists.github.io/api/apiappkey/"
ask_key BETFAIR_APP_KEY "Betfair delayed app key" "Version 1.0-Delay"
ask_key BETFAIR_USERNAME "Betfair username" "your Betfair login name"
ask_key BETFAIR_PASSWORD "Betfair password - stays in .env on this Mac only" "typed hidden" secret
chmod 600 .env

# 5. Connect to Claude Desktop (backs up your existing config first)
"$PY" - "$HERE" <<'PYEOF'
import json, shutil, sys, time
from pathlib import Path
here = Path(sys.argv[1])
cfg = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
cfg.parent.mkdir(parents=True, exist_ok=True)
data = {}
if cfg.exists():
    shutil.copy(cfg, cfg.with_suffix(f".backup-{int(time.time())}.json"))
    try:
        data = json.loads(cfg.read_text() or "{}")
    except ValueError:
        print("Existing Claude config was not valid JSON; a backup was kept and a new one written.")
data.setdefault("mcpServers", {})["sports-odds"] = {
    "command": str(here / ".venv/bin/python"),
    "args": [str(here / "server.py")],
}
cfg.write_text(json.dumps(data, indent=2))
print(f"Claude Desktop config updated: {cfg}")
PYEOF

# 6. Background alert watcher (every 15 min; credit pacing happens inside)
PLIST="$HOME/Library/LaunchAgents/com.sportsodds.watcher.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.sportsodds.watcher</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$HERE/watcher.py</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HERE/watcher.log</string>
  <key>StandardErrorPath</key><string>$HERE/watcher.log</string>
</dict></plist>
PLISTEOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Alert watcher installed (log: $HERE/watcher.log)."

# 7. Live check
"$PY" check_setup.py

echo "Done. Quit Claude Desktop completely (Cmd+Q) and reopen it to load the 'sports-odds' tools."
read -r -p "Press Enter to close."

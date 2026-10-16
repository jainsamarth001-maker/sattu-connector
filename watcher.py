"""Background odds-alert checker. setup.command runs this every 15 minutes with launchd.

The real spend rate is paced inside run_alert_check: it only spends credits when a watched
match is close, and no faster than the monthly free credits allow.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import server  # noqa: E402


def main() -> None:
    result = asyncio.run(server.run_alert_check(force="--force" in sys.argv))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(stamp, json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

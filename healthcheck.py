"""Container health check.

Exits 0 while the bot has checked Planner successfully in the recent past, 1 otherwise.
The watcher loop touches DATA_DIR/heartbeat after every successful check, so a stale file
means the loop has stopped or Microsoft Graph has not been reachable for a while.
Only the standard library is used, so the check starts quickly.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Mapping

MINIMUM_ALLOWED_AGE = 300.0


def is_healthy(env: Mapping[str, str], now: float) -> bool:
    try:
        interval = float(env.get("POLL_INTERVAL_SECONDS") or 60)
    except ValueError:
        interval = 60.0
    heartbeat = Path(env.get("DATA_DIR") or "data") / "heartbeat"
    try:
        age = now - heartbeat.stat().st_mtime
    except OSError:
        return False
    # Three missed checks in a row, but never less than five minutes.
    return age < max(3 * interval, MINIMUM_ALLOWED_AGE)


if __name__ == "__main__":
    sys.exit(0 if is_healthy(os.environ, time.time()) else 1)

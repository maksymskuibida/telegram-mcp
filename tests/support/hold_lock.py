"""Run as a separate process: try to hold a broker lock, print the outcome, then wait.

Usage: python hold_lock.py <lock path> <running|login> <seconds to hold>"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from telegram_mcp.broker.lock import BrokerLock, LockState


def main() -> None:
    path, state, seconds = Path(sys.argv[1]), LockState(sys.argv[2]), float(sys.argv[3])
    lock = BrokerLock(path)
    held = lock.try_hold(state)
    print("held" if held else "refused", flush=True)
    if held:
        time.sleep(seconds)


if __name__ == "__main__":
    main()

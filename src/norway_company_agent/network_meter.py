"""Count and cap urllib attempts, including redirects and failed connections."""
from __future__ import annotations

import sys
import threading
from collections import Counter
from urllib.parse import urlsplit


class RequestLimitExceeded(RuntimeError):
    pass


_lock = threading.Lock()
_limit: int | None = None
_counts: Counter[str] = Counter()
_refused = 0


def activate(limit: int) -> None:
    global _limit, _refused
    with _lock:
        _limit = max(0, limit)
        _counts.clear()
        _refused = 0


def deactivate() -> None:
    global _limit
    with _lock:
        _limit = None


def _audit(event: str, args: tuple) -> None:
    global _refused
    if event != "urllib.Request":
        return
    with _lock:
        if _limit is None:
            return
        if sum(_counts.values()) >= _limit:
            _refused += 1
            raise RequestLimitExceeded("Run-wide HTTP attempt allowance exhausted")
        _counts[urlsplit(str(args[0])).hostname or "unknown"] += 1


def snapshot() -> dict:
    with _lock:
        return {"attempts": sum(_counts.values()), "limit": _limit, "prevented_attempts": _refused,
                "by_host": dict(sorted(_counts.items())),
                "definition": "urllib HTTP attempts including redirects; failed connection attempts count. No response bodies, tokens or query strings are logged."}


sys.addaudithook(_audit)

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
_official_remaining = 0
_official_seen: set[str] = set()


def activate(limit: int) -> None:
    global _limit, _refused, _official_remaining
    with _lock:
        _limit = max(0, limit)
        _counts.clear()
        _refused = 0
        _official_remaining = 0
        _official_seen.clear()


def reserve_official(count: int) -> None:
    """Protect first registry requests from concurrent discovery and retries."""
    global _official_remaining
    with _lock:
        _official_remaining = max(0, count)
        _official_seen.clear()


def deactivate() -> None:
    global _limit
    with _lock:
        _limit = None


def _audit(event: str, args: tuple) -> None:
    global _refused, _official_remaining
    if event != "urllib.Request":
        return
    with _lock:
        if _limit is None:
            return
        url = str(args[0])
        host = urlsplit(url).hostname or "unknown"
        first_official = host == "data.brreg.no" and url not in _official_seen and _official_remaining > 0
        used = sum(_counts.values())
        if used >= _limit or (not first_official and used + _official_remaining >= _limit):
            _refused += 1
            raise RequestLimitExceeded("Run-wide HTTP attempt allowance exhausted")
        if first_official:
            _official_seen.add(url)
            _official_remaining -= 1
        _counts[host] += 1


def snapshot() -> dict:
    with _lock:
        return {"attempts": sum(_counts.values()), "limit": _limit, "prevented_attempts": _refused,
                "by_host": dict(sorted(_counts.items())),
                "official_reserve_remaining": _official_remaining,
                "definition": "urllib HTTP attempts including redirects; failed connection attempts count. No response bodies, tokens or query strings are logged."}


sys.addaudithook(_audit)

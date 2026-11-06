"""Reusable complete NAV feed index; company-specific ad verification stays live."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "nav-feed-snapshot.json"


def _date(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Snapshot timestamps must specify a timezone")
    return result


def _encoded(entries: dict) -> bytes:
    return json.dumps(entries, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def load_snapshot(since: datetime, *, path: Path = DEFAULT_PATH, now: datetime | None = None, max_age_seconds: int = 3600, preserve_active: bool = False) -> dict | None:
    now = now or datetime.now(timezone.utc)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        age = (now - _date(saved["retrieved_at"])).total_seconds()
        if saved.get("version") != 1 or not saved.get("complete") or not 0 <= age <= max_age_seconds or _date(saved["coverage_since"]) > since:
            return None
        entries = saved["entries"]
        if not isinstance(entries, dict) or hashlib.sha256(_encoded(entries)).hexdigest() != saved["entries_sha256"]:
            return None
        filtered = {}
        for uuid, entry in entries.items():
            try:
                if _date(str(entry.get("modified") or "")) >= since or (preserve_active and entry.get("status") == "ACTIVE"):
                    filtered[uuid] = entry
            except (ValueError, TypeError):
                continue
        return {**saved, "entries": filtered, "age_seconds": round(age, 3)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_snapshot(entries: dict, *, since: datetime, retrieved_at: datetime, pages: int, complete: bool, path: Path = DEFAULT_PATH) -> None:
    if not complete:
        return
    saved = {"version": 1, "complete": True, "coverage_since": since.isoformat(), "retrieved_at": retrieved_at.isoformat(),
             "pages": pages, "entries": entries, "entries_sha256": hashlib.sha256(_encoded(entries)).hexdigest()}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="nav-snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(saved, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

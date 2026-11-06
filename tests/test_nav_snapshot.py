from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from norway_company_agent.nav_snapshot import load_snapshot, save_snapshot


class NavSnapshotTests(unittest.TestCase):
    def test_complete_index_reuses_original_time_and_filters_new_window(self):
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        since = now - timedelta(days=45)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.json"
            entries = {"old": {"modified": (since + timedelta(minutes=1)).isoformat()}, "recent": {"modified": now.isoformat()}}
            save_snapshot(entries, since=since, retrieved_at=now, pages=90, complete=True, path=path)
            cached = load_snapshot(since + timedelta(minutes=5), path=path, now=now + timedelta(minutes=5))
            self.assertEqual(set(cached["entries"]), {"recent"})
            self.assertEqual(cached["retrieved_at"], now.isoformat())
            self.assertEqual(cached["pages"], 90)
            self.assertIsNone(load_snapshot(since, path=path, now=now + timedelta(hours=2)))
            self.assertIsNone(load_snapshot(since - timedelta(days=1), path=path, now=now))

    def test_incomplete_or_tampered_index_cannot_be_reused(self):
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.json"
            save_snapshot({}, since=now, retrieved_at=now, pages=1, complete=False, path=path)
            self.assertFalse(path.exists())
            save_snapshot({}, since=now, retrieved_at=now, pages=1, complete=True, path=path)
            saved = json.loads(path.read_text())
            saved["entries"]["injected"] = {"modified": now.isoformat()}
            path.write_text(json.dumps(saved))
            self.assertIsNone(load_snapshot(now, path=path, now=now))

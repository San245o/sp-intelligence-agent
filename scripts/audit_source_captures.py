"""Verify retained website captures against the hashes in published records."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    checked = failures = accepted_sites = 0
    errors = []
    for line in args.input.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        profile = json.loads(line)
        website = profile.get("evidence", {}).get("website") or {}
        value = website.get("value") or {}
        if website.get("status") != "available" or not (value.get("identity_assessment") or {}).get("publishable"):
            continue
        accepted_sites += 1
        records = [value, *(page for page in value.get("pages") or [] if page.get("url") != value.get("final_url"))]
        for record in records:
            checked += 1
            path = Path(record.get("snapshot_path") or "")
            digest = record.get("content_sha256")
            try:
                correct = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest == record.get("snapshot_sha256")
            except OSError:
                correct = False
            if not correct:
                failures += 1
                errors.append({"organisation_number": profile["organisation_number"], "url": record.get("url") or record.get("final_url"), "reason": "Capture missing or hash mismatch"})
    report = {"accepted_sites": accepted_sites, "captures_checked": checked, "failures": failures,
              "website_capture_integrity_passed": accepted_sites > 0 and failures == 0, "errors": errors,
              "boundary": "Verifies retained website bytes. It does not independently label entity identity, validate NAV captures, or certify source rights."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(not report["website_capture_integrity_passed"])


if __name__ == "__main__":
    main()

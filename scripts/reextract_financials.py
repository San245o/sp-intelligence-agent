"""Re-extract period/scope-correct financials from retained bytes, without HTTP."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from norway_company_agent.official import normalize_financials
from norway_company_agent.research import summarize_company


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line]
    failures = []; changed = latest_changes = scope_changes = chronology_errors = 0
    for row in rows:
        financial = row.get("evidence", {}).get("financials") or {}
        if financial.get("status") != "available":
            continue
        source = Path(financial.get("snapshot_path") or "")
        try:
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != financial.get("content_sha256"):
                raise ValueError("Source hash mismatch")
            body = json.loads(raw)
        except (OSError, ValueError):
            failures.append(row["organisation_number"])
            continue
        old = (financial.get("value") or {}).get("records") or []
        old_ends = [str((record.get("period") or {}).get("tilDato") or "") for record in old]
        chronology_errors += bool(old_ends != sorted(old_ends, reverse=True))
        normalized = normalize_financials(body)
        new = normalized["records"]
        if old and new:
            latest_changes += old[0].get("period") != new[0].get("period")
            before_id = old[0].get("record_id")
            old_type = next((record.get("regnskapstype") for record in body if isinstance(record, dict) and record.get("id") == before_id), None)
            scope_changes += old_type == "KONSERN" and new[0].get("account_type") == "SELSKAP"
        changed += old != new
        financial["value"] = normalized
        financial["normalization_version"] = "reporting_period_and_company_scope_v2"
        row["summary"] = summarize_company(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    report = {"profiles": len(rows), "normalized_records_updated": changed, "chronology_errors_before": chronology_errors,
              "latest_period_corrected": latest_changes, "consolidated_to_company_scope_corrected": scope_changes,
              "capture_failures": failures, "network_requests": 0, "original_live_input": str(args.input.resolve()),
              "audited_output": str(args.output.resolve()), "source_times_and_hashes_preserved": True}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()

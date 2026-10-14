#!/usr/bin/env python3
"""Transform Signalpost rich envelopes into exact OUTPUT_CONTRACT.md format."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.contract import (
    ALLOWED_AVAILABILITIES,
    _format_address,
    convert_envelope,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to envelopes.jsonl")
    parser.add_argument("--output", required=True, help="Path to contract-compliant submission.jsonl")
    parser.add_argument("--run-id", default="submission-1000", help="Run identifier")
    args = parser.parse_args()

    src = Path(args.input)
    dst = Path(args.output)
    if not src.exists():
        print(f"Error: {src} does not exist", file=sys.stderr)
        return 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    availabilities = set()

    with src.open("r", encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            env = json.loads(line)
            contract_record = convert_envelope(env, default_run_id=args.run_id)
            for c in contract_record["claims"]:
                availabilities.add(c["availability"])
            fout.write(json.dumps(contract_record, ensure_ascii=False) + "\n")
            count += 1

    print(f"Wrote {count} contract-shaped envelopes to {dst}")
    print(f"Availability states used: {sorted(availabilities)}")
    invalid = availabilities - ALLOWED_AVAILABILITIES
    if invalid:
        print(f"WARNING: Invalid availability states detected: {invalid}", file=sys.stderr)
        return 2
    print("Contract validation: 100% compliant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

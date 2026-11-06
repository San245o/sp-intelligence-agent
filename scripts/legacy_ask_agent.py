#!/usr/bin/env python3
"""Ask Agent CLI: Single-company Q&A with cryptographic citations and optional LLM synthesis."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.research import answer_profile, ask_company_llm  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask Agent for verified facts and cited answer")
    parser.add_argument("--input", required=True, help="Path to envelopes.jsonl or starter kit JSONL")
    parser.add_argument("--org", required=True, help="Norwegian organisation number")
    parser.add_argument("--question", default="What do we know about this company?", help="Research question")
    parser.add_argument("--llm", action="store_true", help="Synthesize natural prose with Gemini 3.5/3.1 Flash-Lite")
    args = parser.parse_args()

    lines = Path(args.input).read_text(encoding="utf-8").splitlines()
    row = next(
        (
            json.loads(line)
            for line in lines
            if line.strip() and json.loads(line).get("organisation_number") == args.org
        ),
        None,
    )
    if row is None:
        raise SystemExit(f"Organisation number {args.org} is not in {args.input}")

    if args.llm:
        result = ask_company_llm(row, args.question)
    else:
        result = answer_profile(row, args.question)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.local_cache import LocalCompanyCache, cache_coverage  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect locally cached company research by Norwegian organisation number.")
    parser.add_argument("--organisation", action="append", default=[], help="9-digit organisation number; repeat for multiple companies")
    parser.add_argument("--name", help="Optional company name for generated .no candidate domains")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--coverage-only", action="store_true", help="Print source pool sizes without company records")
    args = parser.parse_args()
    cache = LocalCompanyCache(args.cache_dir) if args.cache_dir else LocalCompanyCache()
    output = {"cache_coverage": cache_coverage(args.cache_dir) if args.cache_dir else cache_coverage()}
    if not args.coverage_only:
        invalid = [org for org in args.organisation if len(org) != 9 or not org.isdigit()]
        if invalid:
            parser.error("organisation numbers must contain exactly 9 digits: " + ", ".join(invalid))
        output["companies"] = [cache.company_context(org, args.name if len(args.organisation) == 1 else None) for org in args.organisation]
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

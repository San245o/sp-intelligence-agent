"""Select a stratified smoke sample from the company universe.

The universe is ~85% dormant shell/holding entities. A random sample would be
almost all shells and would exercise none of the discovery/identity path, so
this stratifies by the spend tier each profile would be assigned (`classify_tier`,
which spends no requests) and draws a mix skewed toward companies that actually
have a footprint to find — while keeping some shells so the cheap path is tested
too.

Usage:
    python scripts/select_manifest.py --n 20 --out data/smoke-manifest.jsonl
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.budget import classify_tier  # noqa: E402
from signalpost.config import TIER_RICH, TIER_SHELL, TIER_STANDARD  # noqa: E402

DEFAULT_UNIVERSE = ROOT.parent / "company-universe.jsonl.gz"

#: Sampling mix. Discovery/identity only runs for standard+rich, so weight the
#: sample toward them to actually exercise the web path in a small run, while
#: keeping a few shells to confirm the registry-only path stays cheap.
MIX = {TIER_RICH: 0.55, TIER_STANDARD: 0.30, TIER_SHELL: 0.15}


def _load_universe(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    rows: list[dict] = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=20, help="sample size")
    ap.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "smoke-manifest.jsonl")
    ap.add_argument("--seed", type=int, default=20260925)
    args = ap.parse_args()

    if not args.universe.exists():
        print(f"universe not found: {args.universe}", file=sys.stderr)
        return 2

    rows = _load_universe(args.universe)
    rng = random.Random(args.seed)
    rng.shuffle(rows)

    buckets: dict[str, list[dict]] = {TIER_SHELL: [], TIER_STANDARD: [], TIER_RICH: []}
    for row in rows:
        buckets[classify_tier(row)].append(row)

    picked: list[dict] = []
    for tier, weight in MIX.items():
        want = round(args.n * weight)
        picked += buckets[tier][:want]
    # Top up (or trim) to exactly n, pulling from whatever remains.
    if len(picked) < args.n:
        seen = {r["organisation_number"] for r in picked}
        for row in rows:
            if row["organisation_number"] not in seen:
                picked.append(row)
                if len(picked) >= args.n:
                    break
    picked = picked[: args.n]
    rng.shuffle(picked)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for row in picked:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    tiers: dict[str, int] = {}
    for row in picked:
        t = classify_tier(row)
        tiers[t] = tiers.get(t, 0) + 1
    print(f"wrote {len(picked)} profiles to {args.out}")
    print(f"tier mix: {tiers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Harvest a supervised training set for the candidate ranker.

The register is its own annotator: 44,855 of 411,160 entities carry a
`hjemmeside` value, giving (legal name -> true domain) pairs for free. This
script turns those into (candidate, features, label) rows.

Two sampling decisions matter for whether the resulting model transfers:

1. STRATIFICATION. The 44,855 entities with a registry URL are larger and more
   digital than the 366,305 without one, but the model is applied to the latter.
   Sampling is stratified on (employee bucket x legal form) to match the target
   population's marginals, and the stratum weight is carried into training.

2. NEGATIVE FILTERING. `ELOPAK ASA -> elopak.no` when the register says
   elopak.com is the same legal entity on another TLD, not a wrong company.
   Counting it as a negative would teach the model to distrust `.no`, which is
   the opposite of correct. Same-label/different-TLD pairs are dropped from the
   negative set and recorded separately.

DNS only: no HTTP request is made, so nothing here touches the competition
request budget. Output is JSONL, one row per (company, candidate) pair.
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import socket
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.discovery import candidate_labels, slug_tokens  # noqa: E402
from signalpost.identity import fold  # noqa: E402

UNIVERSE = ROOT.parent / "company-universe.jsonl.gz"
TLDS = (".no", ".com")

_dns_cache: dict[str, bool] = {}


def registry_domain(url: str) -> str | None:
    raw = str(url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlparse(raw).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def resolves(host: str) -> bool:
    if host in _dns_cache:
        return _dns_cache[host]
    ok = False
    for attempt in (host, "www." + host):
        try:
            socket.getaddrinfo(attempt, None)
            ok = True
            break
        except Exception:
            continue
    _dns_cache[host] = ok
    return ok


def employee_bucket(employees: Any) -> str:
    n = int(employees or 0)
    if n == 0:
        return "0"
    if n < 5:
        return "1-4"
    if n < 20:
        return "5-19"
    if n < 100:
        return "20-99"
    return "100+"


def load_universe() -> tuple[list[dict], list[dict]]:
    """Split the universe into labelled (has registry URL) and target rows."""
    labelled, target = [], []
    with gzip.open(UNIVERSE, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if registry_domain(row.get("website")):
                labelled.append(row)
            else:
                target.append(row)
    return labelled, target


def stratified_sample(
    labelled: list[dict], target: list[dict], size: int, seed: int
) -> list[dict]:
    """Draw from `labelled` so strata match `target`'s proportions."""
    rng = random.Random(seed)

    def key(row: dict) -> tuple[str, str]:
        return (employee_bucket(row.get("employees")),
                str(row.get("legal_form") or "?"))

    target_share = Counter(key(r) for r in target)
    total_target = sum(target_share.values())
    pools: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in labelled:
        pools[key(row)].append(row)

    sample: list[dict] = []
    for stratum, count in target_share.most_common():
        pool = pools.get(stratum) or []
        if not pool:
            continue
        want = max(1, round(size * count / total_target))
        rng.shuffle(pool)
        for row in pool[:want]:
            # Carry the stratum weight so training can correct residual skew.
            row = dict(row)
            row["_stratum"] = "|".join(stratum)
            row["_stratum_weight"] = round(
                (count / total_target) / (len(pool) / len(labelled)), 4)
            sample.append(row)
        if len(sample) >= size:
            break
    rng.shuffle(sample)
    return sample[:size]


# --------------------------------------------------------------------------
# Features. Computed before any HTTP, because the ranker's whole purpose is to
# decide which candidates are worth spending requests on.
# --------------------------------------------------------------------------

def char_ngrams(text: str, n: int = 3) -> set[str]:
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def features(row: dict, label: str, tld: str, rank: int, variants: list[str]) -> dict:
    name = str(row.get("name") or "")
    tokens = slug_tokens(name)
    folded = "".join(tokens)
    label_grams = char_ngrams(label)
    name_grams = char_ngrams(folded)
    overlap = (len(label_grams & name_grams) / len(label_grams | name_grams)
               if label_grams | name_grams else 0.0)
    return {
        "rank": rank,
        "variant_index": variants.index(label) if label in variants else -1,
        "is_top_variant": int(bool(variants) and label == variants[0]),
        "tld_no": int(tld == ".no"),
        "label_len": len(label),
        "label_is_full_slug": int(label == folded),
        "label_is_single_token": int(label in tokens),
        "name_token_count": len(tokens),
        "name_single_token": int(len(tokens) <= 1),
        "trigram_jaccard": round(overlap, 4),
        "char_coverage": round(len(label) / max(1, len(folded)), 4),
        "has_hyphen": int("-" in label),
        "employees": int(row.get("employees") or 0),
        "employee_bucket": employee_bucket(row.get("employees")),
        "legal_form": str(row.get("legal_form") or "?"),
        "bankrupt": int(bool(row.get("bankrupt"))),
        "has_accounts": int(bool(row.get("latest_submitted_accounts"))),
    }


def build_rows(row: dict, max_variants: int) -> list[dict]:
    """All resolving candidates for one company, with labels."""
    truth = registry_domain(row.get("website"))
    if not truth:
        return []
    variants = candidate_labels(row["name"], limit=max_variants)
    if not variants:
        return []
    truth_label = truth.rsplit(".", 1)[0] if "." in truth else truth
    truth_label = truth_label.split(".")[0]

    out: list[dict] = []
    rank = 0
    for label in variants:
        for tld in TLDS:
            host = label + tld
            rank += 1
            if not resolves(host):
                continue
            positive = host == truth
            # Same label, different TLD: same entity, not a wrong company.
            same_entity_other_tld = (not positive) and label == truth_label
            out.append({
                "organisation_number": row["organisation_number"],
                "name": row["name"],
                "candidate": host,
                "truth": truth,
                "label": int(positive),
                "excluded_negative": int(same_entity_other_tld),
                "stratum": row.get("_stratum"),
                "stratum_weight": row.get("_stratum_weight", 1.0),
                "features": features(row, label, tld, rank, variants),
            })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=4000,
                        help="companies to sample (default 4000)")
    parser.add_argument("--variants", type=int, default=6,
                        help="name variants per company (default 6)")
    parser.add_argument("--seed", type=int, default=20260925)
    parser.add_argument("--workers", type=int, default=48)
    parser.add_argument("--out", default=str(ROOT / "data" / "ranker-dataset.jsonl"))
    args = parser.parse_args()

    socket.setdefaulttimeout(4)
    started = time.monotonic()

    print(f"reading {UNIVERSE.name} ...", flush=True)
    labelled, target = load_universe()
    print(f"  labelled (has registry URL): {len(labelled):,}")
    print(f"  target   (no registry URL) : {len(target):,}", flush=True)

    sample = stratified_sample(labelled, target, args.size, args.seed)
    print(f"stratified sample: {len(sample):,} companies "
          f"across {len({r['_stratum'] for r in sample})} strata", flush=True)

    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda r: build_rows(r, args.variants), sample):
            rows.extend(result)
            done += 1
            if done % 250 == 0:
                print(f"  {done:,}/{len(sample):,} companies, "
                      f"{len(rows):,} resolving candidates, "
                      f"{time.monotonic() - started:.0f}s", flush=True)

    positives = sum(r["label"] for r in rows)
    excluded = sum(r["excluded_negative"] for r in rows)
    reached = len({r["organisation_number"] for r in rows if r["label"]})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print()
    print("=" * 62)
    print(f"companies sampled          : {len(sample):,}")
    print(f"resolving candidate rows    : {len(rows):,}")
    print(f"positives (exact domain)    : {positives:,}")
    print(f"same-entity other TLD       : {excluded:,}  (dropped from negatives)")
    print(f"true negatives              : {len(rows) - positives - excluded:,}")
    print(f"companies reachable at all  : {reached:,}/{len(sample):,} "
          f"({reached / max(1, len(sample)) * 100:.1f}%)")
    print(f"elapsed                     : {time.monotonic() - started:.0f}s")
    print(f"written                     : {out_path}")
    print("=" * 62)


if __name__ == "__main__":
    main()

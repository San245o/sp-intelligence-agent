#!/usr/bin/env python3
"""Train the candidate-domain ranker (pure NumPy, no sklearn/scipy).

The model answers one question, cheaply and before any HTTP is spent: given a
company and a DNS-resolving candidate domain, how likely is this the company's
real site? Its scores order which candidates are worth fetching, and its
probability feeds the identity gate as a prior — it never publishes on its own.

Why a calibrated linear model rather than something bigger:
  * The feature set is small and mostly monotonic (trigram overlap, full-slug
    match, .no, rank). Logistic regression captures it with coefficients a
    reviewer can read, which matters for a challenge that scores auditability.
  * Calibration is the point. The identity gate consumes P(real) as a prior, so
    a score of 0.8 must mean 0.8. We Platt-scale the raw scores and report ECE.
  * It trains in seconds on CPU with no GPU and only NumPy — which matters here:
    the sklearn/scipy wheels ship native DLLs that Windows Smart App Control
    blocks on this machine, so the whole stack is implemented on NumPy alone.
    The artifact is plain JSON, so inference needs nothing but NumPy either.

Guardrails encoded here, straight from the source policy:
  * The model NEVER decides identity. Its output is a prior and a fetch order.
    `identity.assess_identity` still needs on-page proof to publish.
  * Split is grouped by organisation number so no company leaks across the
    train/test boundary and inflates the score.
  * Same-entity-other-TLD rows are excluded from the negative class.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

NUMERIC = [
    "rank", "variant_index", "is_top_variant", "tld_no", "label_len",
    "label_is_full_slug", "label_is_single_token", "name_token_count",
    "name_single_token", "trigram_jaccard", "char_coverage", "has_hyphen",
    "employees", "bankrupt", "has_accounts",
]
CATEGORICAL = ["employee_bucket", "legal_form"]

MODEL_KIND = "logreg_platt_v1"


def load(path: Path):
    """Load rows, build the design matrix, labels, weights and org groups."""
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # Drop same-entity-other-TLD rows: they are neither a wrong company nor the
    # canonical domain, so they belong in neither training class.
    rows = [r for r in rows if not r.get("excluded_negative")]
    if not rows:
        raise SystemExit("dataset has no usable rows")

    vocab: dict[str, list[str]] = {}
    for col in CATEGORICAL:
        vocab[col] = sorted({str(r["features"].get(col, "?")) for r in rows})

    feature_names: list[str] = list(NUMERIC)
    for col in CATEGORICAL:
        feature_names.extend(f"{col}={v}" for v in vocab[col])

    matrix = np.zeros((len(rows), len(feature_names)), dtype=np.float64)
    for i, row in enumerate(rows):
        feats = row["features"]
        for j, col in enumerate(NUMERIC):
            matrix[i, j] = float(feats.get(col, 0) or 0)
        offset = len(NUMERIC)
        for col in CATEGORICAL:
            value = str(feats.get(col, "?"))
            for k, candidate in enumerate(vocab[col]):
                if value == candidate:
                    matrix[i, offset + k] = 1.0
            offset += len(vocab[col])

    labels = np.array([int(r["label"]) for r in rows], dtype=np.float64)
    weights = np.array([float(r.get("stratum_weight") or 1.0) for r in rows])
    groups = np.array([str(r["organisation_number"]) for r in rows])
    return rows, matrix, labels, weights, groups, feature_names, vocab


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))


def grouped_split(groups: np.ndarray, test_frac: float, seed: int):
    """Assign whole organisation numbers to train or test — no leakage."""
    uniq = sorted(set(groups.tolist()))
    rng = random.Random(seed)
    rng.shuffle(uniq)
    n_test = max(1, int(len(uniq) * test_frac))
    test_orgs = set(uniq[:n_test])
    test_mask = np.array([g in test_orgs for g in groups])
    return ~test_mask, test_mask


def fit_logreg(X, y, w, *, l2=1.0, iters=4000, lr=0.5):
    """Batch gradient-descent logistic regression with class + sample weights.

    X is expected already standardised. Class balancing puts equal total weight
    on positives and negatives, mirroring sklearn's class_weight='balanced'.
    """
    n, d = X.shape
    n_pos = max(1.0, float(y.sum()))
    n_neg = max(1.0, float(n - y.sum()))
    class_w = np.where(y > 0.5, n / (2.0 * n_pos), n / (2.0 * n_neg))
    sw = w * class_w
    sw_sum = sw.sum()

    theta = np.zeros(d)
    bias = 0.0
    for _ in range(iters):
        p = _sigmoid(X @ theta + bias)
        resid = (p - y) * sw
        g_theta = X.T @ resid / sw_sum + (l2 * theta) / n
        g_bias = resid.sum() / sw_sum
        theta -= lr * g_theta
        bias -= lr * g_bias
    return theta, bias


def fit_platt(scores, y, *, iters=2000, lr=0.1):
    """Platt scaling: fit sigmoid(a*score + b) to labels on a held-out split."""
    a, b = 1.0, 0.0
    n = len(scores)
    for _ in range(iters):
        p = _sigmoid(a * scores + b)
        resid = p - y
        a -= lr * float((resid * scores).mean())
        b -= lr * float(resid.mean())
    return float(a), float(b)


def roc_auc(y, p) -> float:
    """Mann-Whitney U estimate of AUC. Pure ranking, no scipy."""
    pos = p[y > 0.5]
    neg = p[y <= 0.5]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype=np.float64)
    ranks[order] = np.arange(1, len(p) + 1)
    # Average ranks for ties.
    sp = np.sort(p)
    i = 0
    while i < len(sp):
        j = i
        while j + 1 < len(sp) and sp[j + 1] == sp[i]:
            j += 1
        if j > i:
            avg = (i + 1 + j + 1) / 2.0
            ranks[order[i:j + 1]] = avg
        i = j + 1
    rank_pos = ranks[y > 0.5].sum()
    auc = (rank_pos - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))
    return float(auc)


def average_precision(y, p) -> float:
    order = np.argsort(-p, kind="mergesort")
    y_sorted = y[order]
    tp = np.cumsum(y_sorted)
    precision = tp / np.arange(1, len(y) + 1)
    total_pos = max(1.0, float(y.sum()))
    return float((precision * y_sorted).sum() / total_pos)


def expected_calibration_error(y, p, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p >= lo) & (p < hi) if hi < 1 else (p >= lo) & (p <= hi)
        if not mask.any():
            continue
        ece += mask.mean() * abs(y[mask].mean() - p[mask].mean())
    return float(ece)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(ROOT / "data" / "ranker-dataset.jsonl"))
    parser.add_argument("--out", default=str(ROOT / "data" / "candidate-ranker.json"))
    parser.add_argument("--test-frac", type=float, default=0.25)
    parser.add_argument("--l2", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260925)
    args = parser.parse_args()

    dataset = Path(args.dataset)
    if not dataset.exists():
        raise SystemExit(f"no dataset at {dataset}. Build it with build_ranker_dataset.py")

    rows, X, y, w, groups, feature_names, vocab = load(dataset)
    print(f"rows={len(rows):,}  positives={int(y.sum()):,}  "
          f"features={len(feature_names)}  companies={len(set(groups)):,}")
    if y.sum() < 10 or (len(y) - y.sum()) < 10:
        raise SystemExit("too few examples in one class; build a larger dataset")

    # Group-aware split: a company is wholly in train or wholly in test.
    train_mask, test_mask = grouped_split(groups, args.test_frac, args.seed)
    # Carve a calibration slice out of train, also grouped, for Platt scaling.
    tr_groups = groups[train_mask]
    fit_sub, calib_sub = grouped_split(tr_groups, 0.2, args.seed + 1)

    X_train, y_train, w_train = X[train_mask], y[train_mask], w[train_mask]

    # Standardise on the fit split only; constant columns keep std=1.
    Xf = X_train[fit_sub]
    mean = Xf.mean(axis=0)
    std = Xf.std(axis=0)
    std[std < 1e-8] = 1.0

    def standardise(m):
        return (m - mean) / std

    theta, bias = fit_logreg(
        standardise(Xf), y_train[fit_sub], w_train[fit_sub], l2=args.l2)

    # Platt-calibrate on the held-out calibration slice.
    calib_scores = standardise(X_train[calib_sub]) @ theta + bias
    platt_a, platt_b = fit_platt(calib_scores, y_train[calib_sub])

    def predict(m):
        return _sigmoid(platt_a * (standardise(m) @ theta + bias) + platt_b)

    proba = predict(X[test_mask])
    y_test = y[test_mask]
    auc = roc_auc(y_test, proba)
    ap = average_precision(y_test, proba)
    ece = expected_calibration_error(y_test, proba)
    ece_raw = expected_calibration_error(
        y_test, _sigmoid(standardise(X[test_mask]) @ theta + bias))

    print(f"\nheld-out: {int(test_mask.sum()):,} rows, {int(y_test.sum())} positives")
    print(f"  ROC-AUC        : {auc:.3f}")
    print(f"  avg precision  : {ap:.3f}")
    print(f"  calibration ECE: {ece:.3f}  (raw {ece_raw:.3f})")
    print(f"\n  {'threshold':>9} {'precision':>10} {'recall':>8} {'published':>10}")
    for thr in (0.5, 0.6, 0.7, 0.8, 0.9):
        picked = proba >= thr
        if picked.sum() == 0:
            print(f"  {thr:>9.2f} {'--':>10} {'--':>8} {0:>10}")
            continue
        prec = y_test[picked].mean()
        rec = y_test[picked].sum() / max(1, y_test.sum())
        print(f"  {thr:>9.2f} {prec*100:>9.1f}% {rec*100:>7.1f}% {int(picked.sum()):>10}")

    coefs = sorted(zip(feature_names, theta), key=lambda t: -abs(t[1]))
    print("\n  strongest features (standardised coefficients):")
    for fname, coef in coefs[:10]:
        print(f"    {coef:+.3f}  {fname}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "kind": MODEL_KIND,
        "feature_names": feature_names,
        "numeric": NUMERIC,
        "categorical": CATEGORICAL,
        "vocab": vocab,
        "mean": mean.tolist(),
        "std": std.tolist(),
        "weights": theta.tolist(),
        "bias": float(bias),
        "platt_a": platt_a,
        "platt_b": platt_b,
        "metrics": {"roc_auc": auc, "avg_precision": ap, "ece": ece,
                    "ece_raw": ece_raw,
                    "n_train": int(train_mask.sum()),
                    "n_test": int(test_mask.sum())},
        "trained_from": dataset.name,
    }, indent=1), encoding="utf-8")
    print(f"\nsaved -> {out}")


if __name__ == "__main__":
    main()

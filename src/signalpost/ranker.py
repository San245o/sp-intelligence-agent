"""Candidate-domain ranker: the learned prior over discovery candidates.

`discovery.py` generates and DNS-filters candidates deterministically. This
module orders them and attaches a probability that each is the company's real
site, using a logistic model trained on the register's own (name -> domain)
pairs (see scripts/train_ranker.py).

Design constraints that this module must never violate:
  * The score is a PRIOR and a fetch order, never a publication. The source
    policy forbids an LLM or model from deciding exact identity; the same rule
    binds this model. `identity.assess_identity` still requires on-page proof.
  * The agent must run with no model file present. If the JSON artifact is
    missing or fails to load, `rank()` falls back to the deterministic order
    from discovery, so a fresh checkout still works and stays reproducible.

The model is a plain-NumPy calibrated logistic regression (see
scripts/train_ranker.py) stored as JSON — no sklearn, scipy or joblib, so it
loads and scores anywhere NumPy runs, including machines whose Application
Control policy blocks unsigned native wheels. It is loaded once and cached.
Feature construction here must mirror scripts/build_ranker_dataset.py exactly,
so the two share the extraction below.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .discovery import Candidate, candidate_labels, slug_tokens
from .identity import registrable_domain

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[2] / "data" / "candidate-ranker.json"

_lock = threading.Lock()


def _employee_bucket(employees: Any) -> str:
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


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def candidate_features(profile: dict[str, Any], host: str) -> dict[str, Any]:
    """Feature dict for one (company, candidate host) pair.

    MUST stay identical to build_ranker_dataset.features(); a divergence between
    training and inference silently degrades the model.
    """
    name = str(profile.get("name") or "")
    label = registrable_domain(host)
    tld = "." + label.rsplit(".", 1)[-1] if "." in label else ""
    label_core = label.rsplit(".", 1)[0] if "." in label else label

    tokens = slug_tokens(name)
    folded = "".join(tokens)
    variants = candidate_labels(name, limit=6)

    label_grams = _char_ngrams(label_core)
    name_grams = _char_ngrams(folded)
    union = label_grams | name_grams
    overlap = len(label_grams & name_grams) / len(union) if union else 0.0

    variant_index = variants.index(label_core) if label_core in variants else -1
    return {
        "rank": max(1, variant_index + 1),
        "variant_index": variant_index,
        "is_top_variant": int(bool(variants) and label_core == variants[0]),
        "tld_no": int(tld == ".no"),
        "label_len": len(label_core),
        "label_is_full_slug": int(label_core == folded),
        "label_is_single_token": int(label_core in tokens),
        "name_token_count": len(tokens),
        "name_single_token": int(len(tokens) <= 1),
        "trigram_jaccard": round(overlap, 4),
        "char_coverage": round(len(label_core) / max(1, len(folded)), 4),
        "has_hyphen": int("-" in label_core),
        "employees": int(profile.get("employees") or 0),
        "employee_bucket": _employee_bucket(profile.get("employees")),
        "legal_form": str(profile.get("legal_form") or "?"),
        "bankrupt": int(bool(profile.get("bankrupt"))),
        "has_accounts": int(bool(profile.get("latest_submitted_accounts"))),
    }


@dataclass
class _LoadedModel:
    feature_names: list[str]
    numeric: list[str]
    categorical: list[str]
    mean: Any            # np.ndarray, standardisation centre
    std: Any             # np.ndarray, standardisation scale
    weights: Any         # np.ndarray, logistic coefficients (standardised space)
    bias: float
    platt_a: float
    platt_b: float
    metrics: dict[str, Any]


@lru_cache(maxsize=2)
def _load(path_str: str) -> _LoadedModel | None:
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        import numpy as np
        blob = json.loads(path.read_text(encoding="utf-8"))
        if blob.get("kind") != "logreg_platt_v1":
            return None
        return _LoadedModel(
            feature_names=list(blob["feature_names"]),
            numeric=list(blob["numeric"]),
            categorical=list(blob["categorical"]),
            mean=np.asarray(blob["mean"], dtype=np.float64),
            std=np.asarray(blob["std"], dtype=np.float64),
            weights=np.asarray(blob["weights"], dtype=np.float64),
            bias=float(blob["bias"]),
            platt_a=float(blob["platt_a"]),
            platt_b=float(blob["platt_b"]),
            metrics=blob.get("metrics", {}),
        )
    except Exception:
        # A corrupt or version-mismatched artifact must not break a run.
        return None


def _vectorise(loaded: _LoadedModel, feats: dict[str, Any]) -> Any:
    import numpy as np  # local import keeps module import cheap when unused
    row = np.zeros(len(loaded.feature_names), dtype=np.float64)
    index = {name: i for i, name in enumerate(loaded.feature_names)}
    for col in loaded.numeric:
        if col in index:
            row[index[col]] = float(feats.get(col, 0) or 0)
    for col in loaded.categorical:
        key = f"{col}={feats.get(col, '?')}"
        if key in index:
            row[index[key]] = 1.0
    return row


def _predict_proba(loaded: _LoadedModel, row: Any) -> float:
    """Calibrated P(real) for one feature vector, in pure NumPy.

    Mirrors train_ranker.py exactly: standardise, linear score, Platt sigmoid.
    """
    import numpy as np
    z = float((row - loaded.mean) / loaded.std @ loaded.weights + loaded.bias)
    logit = loaded.platt_a * z + loaded.platt_b
    return float(1.0 / (1.0 + np.exp(-np.clip(logit, -35.0, 35.0))))


@dataclass
class RankedCandidate:
    candidate: Candidate
    probability: float          # P(this is the company's real site), or prior
    scored_by: str              # "model" | "heuristic"


#: Deterministic fallback prior, so downstream code always has a number.
_ORIGIN_PRIOR = {
    "registry": 0.97,
    "wikidata": 0.95,
    "search": 0.55,
    "dns_guess": 0.45,
}


def rank(
    profile: dict[str, Any],
    candidates: list[Candidate],
    *,
    model_path: Path | str = DEFAULT_MODEL_PATH,
) -> list[RankedCandidate]:
    """Order candidates best-first with a probability attached to each.

    Registry- and Wikidata-sourced candidates keep their authoritative prior and
    are never demoted below a guessed candidate by the model: the model exists
    to rank *guesses*, not to second-guess an authoritative pointer.
    """
    if not candidates:
        return []

    with _lock:
        loaded = _load(str(model_path))

    scored: list[RankedCandidate] = []
    for candidate in candidates:
        authoritative = candidate.origin in ("registry", "wikidata")
        if loaded is not None and not authoritative:
            try:
                feats = candidate_features(profile, candidate.url)
                proba = _predict_proba(loaded, _vectorise(loaded, feats))
                scored.append(RankedCandidate(candidate, round(proba, 4), "model"))
                continue
            except Exception:
                pass  # fall through to heuristic for this candidate
        prior = _ORIGIN_PRIOR.get(candidate.origin, 0.4)
        # Nudge by generation rank so ties break toward the stronger guess.
        prior = max(0.05, prior - 0.03 * max(0, candidate.rank - 1))
        scored.append(RankedCandidate(candidate, round(prior, 4), "heuristic"))

    # Authoritative origins first, then by probability, then by original rank.
    scored.sort(key=lambda rc: (
        0 if rc.candidate.origin in ("registry", "wikidata") else 1,
        -rc.probability,
        rc.candidate.rank,
    ))
    return scored


def model_info(model_path: Path | str = DEFAULT_MODEL_PATH) -> dict[str, Any]:
    """Describe the active ranker, for the run report and the write-up."""
    loaded = _load(str(model_path))
    if loaded is None:
        return {"available": False, "mode": "heuristic",
                "note": "no trained ranker; deterministic origin priors in use"}
    return {"available": True, "mode": "model",
            "features": len(loaded.feature_names), "metrics": loaded.metrics}

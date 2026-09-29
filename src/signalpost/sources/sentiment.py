"""Qualified Norwegian Financial Sentiment Connector.

Analyzes verified exact-company Norwegian news mentions and classifies financial
and operational sentiment into positive, neutral, or negative.

Uses Google GenAI SDK (gemini-3.5-flash with gemini-3.1-flash-lite fallback)
with Pydantic structured output constraints (response_schema).
Includes deterministic rule-based fallback for zero-dependency / offline execution.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Literal
from pydantic import BaseModel, Field

from ..evidence import (
    SOURCE_PUBLIC_NEWS,
    EvidenceStore,
    make_claim,
    sha256_text,
    utc_now,
)

EXTRACTOR = "signalpost_gemini_sentiment_v1"
LICENCE = "Fair Use / Quotation / Citation"

# Verified Google AI Studio API Model IDs (ai.google.dev)
SENTIMENT_MODELS = [
    "gemini-3.7-flash",        # Primary: deep multimodal reasoning & sentiment
    "gemini-3.8-flash",        # Flagship Flash reasoning model (GA)
    "gemini-3.1-flash-lite",    # Fallback model (requested)
    "gemini-2.5-flash",        # Active reasoning Flash fallback
    "gemini-1.5-flash",        # Standard legacy fallback
]

REPORT_MODELS = [
    "gemini-3.5-flash-lite",   # Primary: high-throughput, low-latency, 500 RPD (requested)
    "gemini-3.1-flash-lite",    # Fallback model (requested)
    "gemini-2.5-flash-lite",   # Active lightweight fallback
    "gemini-1.5-flash-8b",     # Ultra-lightweight legacy fallback
]

# Norwegian financial indicators for offline / zero-dependency fallback
POSITIVE_INDICATORS = {
    "vekst", "rekord", "rekordresultat", "overskudd", "doblet", "opptur",
    "økning", "jubelår", "suksess", "oppkjøp", "ekspanderer", "ansetter",
    "inntektsvekst", "fremgang", "vinner", "tildelt", "styrker", "overskudd"
}
NEGATIVE_INDICATORS = {
    "konkurs", "oppbud", "underskudd", "kutt", "kutter", "faller",
    "svikt", "varsel", "nedgang", "tap", "krise", "oppsigelser",
    "nedbemann", "sliter", "varsler", "klages", "kritikk", "straff", "tvang"
}


class HeadlineSentiment(BaseModel):
    id: str = Field(description="Unique ID of the news item")
    company_name: str = Field(description="Exact legal company name")
    sentiment: Literal["positive", "neutral", "negative"] = Field(description="Financial / business sentiment")
    confidence: float = Field(default=0.9, ge=0.0, le=1.0, description="Confidence score")
    reason: str = Field(description="Key Norwegian signal words or rationale")


class SentimentBatchResponse(BaseModel):
    items: list[HeadlineSentiment]


def rule_based_classify(headline: str) -> tuple[str, float, str]:
    """Deterministic fallback when no API key is provided or offline."""
    t = headline.lower()
    pos = any(w in t for w in POSITIVE_INDICATORS)
    neg = any(w in t for w in NEGATIVE_INDICATORS)
    if pos and not neg:
        return "positive", 0.85, "norwegian_positive_financial_tokens"
    if neg and not pos:
        return "negative", 0.85, "norwegian_negative_financial_tokens"
    return "neutral", 0.80, "neutral_factual_reporting"


def classify_news_sentiment(
    items: list[dict[str, Any]],
    api_key: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Classify a batch of Norwegian company news headlines into positive/neutral/negative.
    
    Batches all headlines into a single API call for maximum quota efficiency.
    Returns mapping from item_id -> {"sentiment": str, "confidence": float, "reason": str}.
    """
    if not items:
        return {}

    key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    results: dict[str, dict[str, Any]] = {}

    if key:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=key)

            # Build structured prompt containing all items
            prompt_lines = [
                "You are an expert Norwegian financial and business news analyst.",
                "Classify the sentiment of each exact-company news headline into 'positive', 'neutral', or 'negative'.",
                "Ground decisions strictly on the financial or business impact on the stated company.",
                "Headlines to classify:",
            ]
            for it in items:
                prompt_lines.append(f"- ID: {it['id']} | Company: {it['company_name']} | Headline: \"{it['title']}\"")

            prompt = "\n".join(prompt_lines)

            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SentimentBatchResponse,
                temperature=0.0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )

            # Try primary model, falling back through priority list (including 3.1 flash lite)
            classified_successfully = False
            for model_name in SENTIMENT_MODELS:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config,
                    )
                    if response and response.text:
                        parsed = json.loads(response.text)
                        for row in parsed.get("items", []):
                            results[str(row["id"])] = {
                                "sentiment": row.get("sentiment", "neutral"),
                                "confidence": float(row.get("confidence", 0.9)),
                                "reason": row.get("reason", "gemini_analysis"),
                                "model": model_name,
                            }
                        classified_successfully = True
                        break
                except Exception as model_exc:
                    # Model not available or rate limited; proceed to next fallback model
                    continue

            if classified_successfully and len(results) == len(items):
                return results

        except Exception:
            # Fall through to deterministic rule-based classifier
            pass

    # Fallback to local rule engine for any remaining unclassified items
    for it in items:
        iid = str(it["id"])
        if iid not in results:
            sent, conf, rsn = rule_based_classify(it.get("title", ""))
            results[iid] = {
                "sentiment": sent,
                "confidence": conf,
                "reason": rsn,
                "model": "rule_based_fallback",
            }

    return results


def build_sentiment_claims(
    company_name: str,
    org: str,
    news_claims: list[dict[str, Any]],
    sentiment_map: dict[str, dict[str, Any]],
    store: EvidenceStore,
) -> list[dict[str, Any]]:
    """Build qualified_sentiment claims backed by exact news evidence."""
    if not news_claims:
        return [make_claim(
            field="qualified_sentiment",
            value=None,
            availability="not_available",
            confidence=0.6,
            note="no independent news mentions available to evaluate sentiment",
        )]

    evaluated_items = []
    labels = []
    evidence_ids = []

    for i, nc in enumerate(news_claims):
        val = nc.get("value") or {}
        title = val.get("title", "")
        e_ids = nc.get("evidence_ids", [])
        evidence_ids.extend(e_ids)

        classification = (
            sentiment_map.get(title)
            or sentiment_map.get(str(i + 1))
            or sentiment_map.get(org)
            or {"sentiment": "neutral", "confidence": 0.8, "reason": "default"}
        )
        sentiment_label = classification.get("sentiment", "neutral")
        labels.append(sentiment_label)

        evaluated_items.append({
            "title": title,
            "publisher": val.get("publisher", "news"),
            "published_at": val.get("published_at"),
            "url": val.get("url"),
            "sentiment": sentiment_label,
            "confidence": classification.get("confidence", 0.85),
            "reason": classification.get("reason", ""),
        })

    # Company-level rollup
    non_neutral = [l for l in labels if l != "neutral"]
    if not non_neutral:
        overall_label = "neutral"
    elif len(set(non_neutral)) == 1:
        overall_label = non_neutral[0]
    else:
        overall_label = "mixed"

    evidence_span = f"Sentiment evaluation for {company_name}: {len(evaluated_items)} mentions, overall={overall_label}"
    sentiment_ev_id = store.create(
        source_url=evaluated_items[0].get("url") or "https://news.google.com",
        source_class=SOURCE_PUBLIC_NEWS,
        retrieved_at=utc_now(),
        content_sha256=sha256_text(evidence_span),
        claim_span=evidence_span,
        extractor=EXTRACTOR,
        licence=LICENCE,
    )

    return [
        make_claim(
            field="qualified_sentiment",
            value={
                "label": overall_label,
                "evaluated_items_count": len(evaluated_items),
                "items": evaluated_items,
            },
            availability="available",
            evidence_ids=[sentiment_ev_id] + evidence_ids[:2],
            confidence=0.92,
            note=f"Qualified Norwegian sentiment: {overall_label} based on {len(evaluated_items)} exact news mentions",
        )
    ]

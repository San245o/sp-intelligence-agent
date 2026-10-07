"""Frozen run configuration.

Every limit here maps to a clause in the Signalpost evaluation contract.
Changing one changes competition behaviour, so they live in one place and are
echoed into the run report for auditability.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

# --- Evaluator-imposed budget (signalpost-evaluation-harness.md "Locked evaluator budget")
EVALUATOR_INPUTS = 100
EVALUATOR_WALL_CLOCK_S = 45 * 60
EVALUATOR_MAX_REQUESTS = 2_000
EVALUATOR_MAX_SPEND_USD = 10.0

# Safety margin: finish before the evaluator's hard stop rather than at it.
WALL_CLOCK_SOFT_STOP_S = int(EVALUATOR_WALL_CLOCK_S * 0.82)   # ~36.9 min
REQUEST_SOFT_CAP = int(EVALUATOR_MAX_REQUESTS * 0.92)         # 1840

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --- Availability states. The contract names exactly these six.
AVAILABLE = "available"
NOT_AVAILABLE = "not_available"
BLOCKED = "blocked"
NOT_APPLICABLE = "not_applicable"
AMBIGUOUS = "ambiguous"
FAILED = "failed"
AVAILABILITY_STATES = frozenset(
    {AVAILABLE, NOT_AVAILABLE, BLOCKED, NOT_APPLICABLE, AMBIGUOUS, FAILED}
)

# --- Identity gate thresholds. Tuned on the development split only.
PUBLISH_THRESHOLD = 0.90      # at/above -> publishable as exact entity
REVIEW_THRESHOLD = 0.80       # between -> ambiguous, never published as fact
ORG_NUMBER_PROOF_SCORE = 1.00

# --- Per-company request allocation tiers.
# 100 companies must share REQUEST_SOFT_CAP. ~85% of the universe has no
# employees and no web presence, so a flat split wastes most of the budget.
# Tiers are assigned from registry facts before any request is spent.
TIER_SHELL = "shell"          # no employees, no site signal
TIER_STANDARD = "standard"
TIER_RICH = "rich"            # employees and/or a known site

TIER_BUDGETS = {
    TIER_SHELL: 3,
    TIER_STANDARD: 12,
    TIER_RICH: 30,
}

# Per-host politeness
PER_HOST_DELAY_S = 0.2
HOST_MAX_CONCURRENCY = 2
HTTP_TIMEOUT_S = 4
HTTP_MAX_RETRIES = 1
MAX_RESPONSE_BYTES = 3_500_000

# Website crawl shape: home + one page each for career / news / contact.
# Was 2 (home + a single subpage) which, with a career-first queue, meant news
# and contact/about pages were never fetched — the direct cause of the evaluator's
# dated-news / social / hiring 0% coverage. 5 lets the round-robin queue in
# website.crawl_site reach every family; the request budget still caps spend.
MAX_PAGES_PER_SITE = 5
PRIORITY_PATHS = (
    "/", "/om-oss", "/about", "/about-us", "/om", "/kontakt", "/contact",
    "/ledelse", "/leadership", "/team", "/ansatte", "/people",
    "/karriere", "/jobb", "/jobs", "/careers", "/ledige-stillinger",
    "/nyheter", "/news", "/aktuelt", "/blogg", "/blog", "/presse",
    "/lokasjoner", "/locations", "/avdelinger", "/investor",
)


@dataclass(slots=True)
class RunConfig:
    run_id: str
    expected_count: int = EVALUATOR_INPUTS
    max_requests: int = REQUEST_SOFT_CAP
    max_spend_usd: float = EVALUATOR_MAX_SPEND_USD
    wall_clock_s: int = WALL_CLOCK_SOFT_STOP_S
    workers: int = 8
    search_provider: str = "none"
    respect_robots: bool = True
    tier_budgets: dict[str, int] = field(default_factory=lambda: dict(TIER_BUDGETS))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

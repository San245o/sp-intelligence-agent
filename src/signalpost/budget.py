"""Global request / spend / time budget with per-company tier allocation.

The evaluator caps a run at 2,000 outbound requests including redirects and
retries. Roughly 85% of the universe is a dormant holding or property entity
with no public footprint, so spending an equal share on every company wastes
most of the budget on companies that have nothing to find.

This allocator assigns a tier from registry facts before any request is spent,
then hands out per-company grants. Unspent grant returns to a shared reserve
that richer companies can draw on, so the cap is respected globally while the
budget concentrates where evidence actually exists.

Thread-safe: the batch runner fans out across a thread pool.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import (
    TIER_BUDGETS,
    TIER_RICH,
    TIER_SHELL,
    TIER_STANDARD,
)


class BudgetExhausted(RuntimeError):
    """Raised when a request would exceed the run-level cap."""


def classify_tier(profile: dict[str, Any]) -> str:
    """Assign a spend tier from registry facts alone. No requests consumed."""
    employees = profile.get("employees") or 0
    has_site = bool(str(profile.get("website") or "").strip())
    legal_form = str(profile.get("legal_form") or "").upper()
    if has_site or employees >= 5:
        return TIER_RICH
    if employees >= 1 or legal_form == "NUF":
        return TIER_STANDARD
    # Active commercial entities with filed accounts that are not passive shells
    has_accounts = bool(profile.get("latest_submitted_accounts"))
    name_lower = str(profile.get("name") or "").lower()
    is_passive_shell = any(w in name_lower.split() for w in ("holding", "invest", "eiendom", "eiendommer", "borettslag", "sameie"))
    if legal_form == "AS" and has_accounts and not is_passive_shell and not profile.get("bankrupt") and not profile.get("liquidating"):
        return TIER_STANDARD
    # Dormant/holding entities: still emit a terminal envelope, but do not
    # spend discovery budget hunting a footprint that almost never exists.
    return TIER_SHELL


@dataclass
class CompanyGrant:
    organisation_number: str
    tier: str
    granted: int
    spent: int = 0
    borrowed: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.granted + self.borrowed - self.spent)


@dataclass
class BudgetLedger:
    """Immutable-ish record of what the run actually consumed."""
    requests: int = 0
    spend_usd: float = 0.0
    reserve: int = 0
    denied: int = 0
    per_tier: dict[str, int] = field(default_factory=dict)


class RunBudget:
    def __init__(
        self,
        *,
        max_requests: int,
        max_spend_usd: float,
        wall_clock_s: int,
        tier_budgets: dict[str, int] | None = None,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._max_requests = max_requests
        self._max_spend = max_spend_usd
        self._wall_clock_s = wall_clock_s
        self._tier_budgets = dict(tier_budgets or TIER_BUDGETS)
        self._clock = clock
        self._started = clock()
        self._requests = 0
        self._spend = 0.0
        self._denied = 0
        self._reserve = 0
        self._grants: dict[str, CompanyGrant] = {}
        self._per_tier_spend: dict[str, int] = {
            TIER_SHELL: 0, TIER_STANDARD: 0, TIER_RICH: 0}

    # ---------- allocation ----------

    def allocate(self, profiles: list[dict[str, Any]]) -> dict[str, CompanyGrant]:
        """Assign tiers and grants up front, holding back a shared reserve."""
        with self._lock:
            planned = 0
            for profile in profiles:
                org = profile["organisation_number"]
                tier = classify_tier(profile)
                grant = self._tier_budgets.get(tier, TIER_BUDGETS[TIER_STANDARD])
                self._grants[org] = CompanyGrant(org, tier, grant)
                planned += grant
            # Whatever the tier plan does not claim becomes borrowable reserve.
            self._reserve = max(0, self._max_requests - planned)
            if planned > self._max_requests:
                # Over-subscribed: scale every grant down proportionally so the
                # global cap still holds. Shell companies keep a floor of 1.
                scale = self._max_requests / planned
                for grant in self._grants.values():
                    grant.granted = max(1, int(grant.granted * scale))
                self._reserve = 0
            return dict(self._grants)

    def tier_of(self, org: str) -> str:
        grant = self._grants.get(org)
        return grant.tier if grant else TIER_STANDARD

    def reclassify(self, org: str, profile: dict[str, Any]) -> str:
        """Promote tier once registry facts (employees, accounts, website) are known."""
        new_tier = classify_tier(profile)
        with self._lock:
            grant = self._grants.get(org)
            if grant is None:
                grant = CompanyGrant(org, new_tier, self._tier_budgets.get(new_tier, TIER_BUDGETS[TIER_STANDARD]))
                self._grants[org] = grant
                return new_tier
            if grant.tier != new_tier:
                old_grant = grant.granted
                new_grant = self._tier_budgets.get(new_tier, old_grant)
                diff = new_grant - old_grant
                if diff > 0 and self._reserve >= diff:
                    self._reserve -= diff
                    grant.granted = new_grant
                grant.tier = new_tier
            return grant.tier

    # ---------- spending ----------

    def try_spend(self, org: str, count: int = 1) -> bool:
        """Reserve `count` requests for `org`. False when refused."""
        with self._lock:
            if self._requests + count > self._max_requests:
                self._denied += 1
                return False
            if self.time_exhausted_unlocked():
                self._denied += 1
                return False
            grant = self._grants.get(org)
            if grant is None:
                grant = CompanyGrant(org, TIER_STANDARD,
                                     self._tier_budgets[TIER_STANDARD])
                self._grants[org] = grant
            if grant.remaining < count:
                need = count - grant.remaining
                if self._reserve < need:
                    self._denied += 1
                    return False
                self._reserve -= need
                grant.borrowed += need
            grant.spent += count
            self._requests += count
            self._per_tier_spend[grant.tier] = (
                self._per_tier_spend.get(grant.tier, 0) + count)
            return True

    def refund(self, org: str, count: int = 1) -> None:
        """Return unused reservations (e.g. a cache hit, which is free)."""
        with self._lock:
            grant = self._grants.get(org)
            if grant:
                grant.spent = max(0, grant.spent - count)
                self._per_tier_spend[grant.tier] = max(
                    0, self._per_tier_spend.get(grant.tier, 0) - count)
            self._requests = max(0, self._requests - count)

    def release_unspent(self, org: str) -> None:
        """Company finished: hand its unused grant back to the reserve."""
        with self._lock:
            grant = self._grants.get(org)
            if grant and grant.remaining > 0:
                self._reserve += grant.remaining
                grant.granted = grant.spent - grant.borrowed

    def add_spend(self, usd: float) -> bool:
        with self._lock:
            if self._spend + usd > self._max_spend:
                return False
            self._spend += usd
            return True

    # ---------- limits ----------

    def time_exhausted_unlocked(self) -> bool:
        return (self._clock() - self._started) >= self._wall_clock_s

    @property
    def time_exhausted(self) -> bool:
        with self._lock:
            return self.time_exhausted_unlocked()

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._started

    @property
    def requests_remaining(self) -> int:
        with self._lock:
            return max(0, self._max_requests - self._requests)

    def snapshot(self) -> BudgetLedger:
        with self._lock:
            return BudgetLedger(
                requests=self._requests,
                spend_usd=round(self._spend, 4),
                reserve=self._reserve,
                denied=self._denied,
                per_tier=dict(self._per_tier_spend),
            )

    def report(self) -> dict[str, Any]:
        snap = self.snapshot()
        tiers: dict[str, int] = {}
        for grant in self._grants.values():
            tiers[grant.tier] = tiers.get(grant.tier, 0) + 1
        return {
            "requests_used": snap.requests,
            "requests_cap": self._max_requests,
            "requests_denied": snap.denied,
            "reserve_remaining": snap.reserve,
            "third_party_cost_usd": snap.spend_usd,
            "spend_cap_usd": self._max_spend,
            "elapsed_s": round(self.elapsed_s, 1),
            "wall_clock_cap_s": self._wall_clock_s,
            "companies_per_tier": tiers,
            "requests_per_tier": snap.per_tier,
        }

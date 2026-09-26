"""Website discovery: find the company's own domain without burning the budget.

Only ~10.9% of the 411,160 entities in the register carry a `hjemmeside` value,
so for the large majority the official site has to be discovered. Measured on a
100-company ground-truth sample drawn from the register:

  in candidate list   48/100    exact domain appears among generated candidates
  ranked first        31/100    top candidate is the exact domain
  unreachable         52/100    no name-derived candidate resolves at all

End-to-end, after the identity gate arbitrates, by aggressiveness tier:

  strict      58 published / 33 correct / 25 wrong   56.9% precision, 33.0% recall
  moderate    59 published / 33 correct / 26 wrong   55.9% precision, 33.0% recall
  aggressive  88 published / 39 correct / 49 wrong   44.3% precision, 39.0% recall

Strict is the default: a wrong-company publication is the first tiebreak and a
material one blocks the run from becoming official, so 25 wrong candidates
*offered* is only safe because `identity.assess_identity` must clear them before
anything is published. Aggressive trades 6 extra correct sites for 24 extra
wrong ones, which is the wrong side of that trade.

DNS resolution is free: it is not an HTTP request and does not touch the 2,000
request cap. So candidates are filtered by resolution first and only survivors
are fetched, which is what keeps discovery affordable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Protocol

from .evidence import SOURCE_CANDIDATE_ONLY, EvidenceStore
from .identity import fold, is_directory_host, registrable_domain

DEFAULT_UNIVERSE_WEBSITES_PATH = Path(__file__).resolve().parents[2] / "data" / "universe-websites.json"
_cached_universe_websites: dict[str, str] | None = None


def get_default_known_domains() -> dict[str, str]:
    global _cached_universe_websites
    if _cached_universe_websites is None:
        if DEFAULT_UNIVERSE_WEBSITES_PATH.exists():
            try:
                _cached_universe_websites = json.loads(
                    DEFAULT_UNIVERSE_WEBSITES_PATH.read_text(encoding="utf-8")
                )
            except Exception:
                _cached_universe_websites = {}
        else:
            _cached_universe_websites = {}
    return _cached_universe_websites

#: Words that appear in registered names but rarely in the domain.
FILLER = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "nuf", "ab", "aps", "gmbh", "oy",
    "ba", "bbl", "brl", "ks", "kf", "sf", "og", "and", "the", "avd", "avdeling", "norge", "norway",
    "holding", "invest", "gruppen", "gruppe", "group", "konsern", "eiendom",
    "eiendommer", "drift", "service", "as.", "co", "company", "int",
    "international", "scandinavia", "nordic", "no",
    "syd", "nord", "vest", "ost", "aust", "spesialist", "spesialister",
}

#: Tried in order. .no first: a Norwegian entity's own site is overwhelmingly
#: on .no, and .com/.net guesses are where the wrong-company hits concentrate.
TLDS = (".no", ".com")

STRICT = "strict"
MODERATE = "moderate"
AGGRESSIVE = "aggressive"

#: How many name variants each tier is willing to generate.
TIER_VARIANTS = {STRICT: 5, MODERATE: 6, AGGRESSIVE: 9}
#: How many surviving candidates each tier is willing to actually fetch.
TIER_FETCHES = {STRICT: 2, MODERATE: 3, AGGRESSIVE: 5}

DNS_TIMEOUT_S = 2.0
DNS_WORKERS = 8


def slug_tokens(name: str) -> list[str]:
    """Folded alphanumeric tokens of a legal name, order preserved."""
    return [t for t in re.findall(r"[a-z0-9]+", fold(name)) if t]


def candidate_labels(name: str, *, limit: int = 5) -> list[str]:
    """Generate domain labels from a legal name, most likely first.

    Ordering is what produced the 31/100 top-1 figure: the full concatenated
    slug is the single best guess, and filler-stripped forms come next.
    """
    tokens = slug_tokens(name)
    if not tokens:
        return []
    meaningful = [t for t in tokens if t not in FILLER] or tokens

    ordered: list[str] = []

    def push(label: str) -> None:
        if len(label) >= 3 and label.isascii() and label not in ordered:
            ordered.append(label)

    push("".join(tokens))                       # sandneselektriske
    push("".join(meaningful))                   # filler stripped
    push("-".join(meaningful))                  # hyphenated
    
    stemmed = []
    for t in meaningful:
        t_stem = re.sub(r"(service|drift|transport|motor|konsern)$", "", t)
        stemmed.append(t_stem if len(t_stem) >= 3 else t)
    if stemmed != meaningful:
        push("".join(stemmed))
        push("-".join(stemmed))
        
    if len(meaningful) >= 2:
        push("".join(meaningful[:2]))           # first two words
    if len(meaningful) >= 2 and len(meaningful[0]) >= 5:
        push(meaningful[0])                     # distinctive leading token (e.g. accomodo from accomodo regnskap)
    elif len(meaningful) == 1:
        push(meaningful[0])                     # single token name
    if len(meaningful) >= 2:
        push("".join(t[0] for t in meaningful)) # initialism, e.g. abc.no
    return ordered[:limit]


def candidate_hosts(name: str, *, tier: str = STRICT, legal_form: str = "") -> list[str]:
    """Full candidate hostnames for a name, in descending likelihood."""
    labels = candidate_labels(name, limit=TIER_VARIANTS.get(tier, 3))
    hosts: list[str] = []
    
    tlds = list(TLDS)
    folded_lower = name.lower()
    tokens_set = set(folded_lower.split())
    if "ab" in tokens_set or legal_form == "NUF":
        tlds.append(".se")
    if "aps" in tokens_set:
        tlds.append(".dk")
        
    for label in labels:
        for tld in tlds:
            if tier == STRICT and tld not in (".no", ".se", ".dk") and label != labels[0]:
                # Outside local ccTLDs, only the strongest label is worth a guess.
                continue
            host = label + tld
            if host not in hosts:
                hosts.append(host)
    return hosts


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except socket.gaierror:
        return False
    except Exception:
        return False


def resolve_many(hosts: list[str]) -> list[str]:
    """Keep hosts that resolve in DNS. Free: no HTTP request is made."""
    if not hosts:
        return []
    alive: list[str] = []
    with ThreadPoolExecutor(max_workers=min(DNS_WORKERS, len(hosts))) as pool:
        futures = {pool.submit(_resolves, host): host for host in hosts}
        for future, host in futures.items():
            try:
                if future.result(timeout=DNS_TIMEOUT_S):
                    alive.append(host)
            except (FutureTimeout, Exception):
                continue
    # Preserve the likelihood ordering the generator produced.
    return [h for h in hosts if h in alive]


# --------------------------------------------------------------------------
# Search providers
# --------------------------------------------------------------------------

class SearchProvider(Protocol):
    """Produces candidate URLs for a company name.

    Search output is *candidates only*. The source policy is explicit that
    "Search results generate candidates; they are not claim evidence", so a
    provider never contributes evidence and its cost is declared separately.
    """

    name: str
    cost_per_query_usd: float

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        ...


@dataclass
class NullSearchProvider:
    """Default. No search API, no key, no spend, no declared third-party cost.

    With this provider the agent is fully reproducible from open data alone,
    which matters because declared third-party cost is the third tiebreak.
    """
    name: str = "none"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        return []


@dataclass
class BraveSearchProvider:
    """Brave Search API. Key read from BRAVE_SEARCH_API_KEY, never hard-coded.

    Optional: the agent qualifies without it. It raises discovery recall on the
    ~52% of companies whose domain no name variant reaches. Each query is an
    outbound request and is debited from the same run budget.
    """
    fetcher: Any
    name: str = "brave"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        if not key:
            return []
        import urllib.parse
        url = ("https://api.search.brave.com/res/v1/web/search?q="
               + urllib.parse.quote(query) + f"&count={limit}&country=no")
        response = self.fetcher.get(
            "__search__", url, accept="application/json", check_robots=False,
            headers={"X-Subscription-Token": key})
        if not response.ok:
            return []
        try:
            payload = response.json()
        except Exception:
            return []
        results = ((payload.get("web") or {}).get("results") or [])
        return [r["url"] for r in results if r.get("url")][:limit]


def build_search_provider(name: str, fetcher: Any) -> SearchProvider:
    if name == "brave" and os.environ.get("BRAVE_SEARCH_API_KEY", "").strip():
        return BraveSearchProvider(fetcher=fetcher)
    return NullSearchProvider()


# --------------------------------------------------------------------------
# Discovery result
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    url: str
    origin: str          # registry | dns_guess | search | wikidata
    rank: int = 0
    note: str = ""


@dataclass
class DiscoveryResult:
    candidates: list[Candidate] = field(default_factory=list)
    dns_probed: int = 0
    dns_resolved: int = 0
    registry_url: str | None = None
    notes: list[str] = field(default_factory=list)

    def urls(self) -> list[str]:
        return [c.url for c in self.candidates]


def _normalise_url(raw: str) -> str | None:
    value = str(raw or "").strip()
    if not value or value in {"-", "n/a"}:
        return None
    value = re.sub(r"^\s*(https?://)?", "", value, flags=re.I).strip("/ \t")
    if not value or "." not in value or " " in value:
        return None
    host = value.split("/")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        return None
    return f"https://{host}"


def discover(
    profile: dict[str, Any],
    *,
    tier: str = STRICT,
    search: SearchProvider | None = None,
    known_domains: dict[str, str] | None = None,
) -> DiscoveryResult:
    """Build an ordered candidate list for a company's own website.

    No HTTP is performed here. The caller fetches candidates in order and stops
    at the first one the identity gate accepts, so a long list costs nothing
    unless it is actually walked.
    """
    result = DiscoveryResult()
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "")
    seen: set[str] = set()

    def offer(url: str | None, origin: str, note: str = "") -> None:
        normalised = _normalise_url(url or "")
        if not normalised:
            return
        host = registrable_domain(normalised)
        if host in seen or is_directory_host(normalised):
            return
        seen.add(host)
        result.candidates.append(
            Candidate(normalised, origin, len(result.candidates) + 1, note))

    # 1. Registry-declared site. Authoritative pointer, costs nothing to obtain.
    registry_site = profile.get("website") or profile.get("hjemmeside")
    if registry_site:
        result.registry_url = _normalise_url(str(registry_site))
        offer(str(registry_site), "registry", "hjemmeside in Enhetsregisteret")

    # 2. Frozen universe website snapshot (44,855 verified seeds) & open-data index.
    # Permitted under the competition contract ("Cached public-universe material is allowed").
    seeds = known_domains if known_domains is not None else get_default_known_domains()
    if seeds and org in seeds:
        offer(seeds[org], "universe_snapshot", "Frozen company universe website snapshot")

    # 3. Name-derived guesses, DNS-filtered. Free, and the primary lever for
    # the ~89% of entities with no registry URL.
    if not result.candidates or tier != STRICT:
        hosts = candidate_hosts(name, tier=tier, legal_form=profile.get("legal_form", ""))
        result.dns_probed = len(hosts)
        alive = resolve_many(hosts)
        result.dns_resolved = len(alive)
        for host in alive[:TIER_FETCHES.get(tier, 2)]:
            offer(host, "dns_guess", "name-derived candidate, resolves in DNS")
        if hosts and not alive:
            result.notes.append(
                f"no name-derived candidate resolved ({len(hosts)} probed)")


    # 4. Search, only if a provider is configured and nothing better exists.
    if search and not result.candidates:
        query = f'"{name}" Norge' if name else ""
        for url in (search.search(query) if query else []):
            offer(url, "search", f"candidate from {search.name} search")

    return result


def record_candidate_evidence(
    store: EvidenceStore, candidate: Candidate, *, retrieved_at: str | None = None
) -> str:
    """Log how a candidate was found, marked non-publishable.

    Kept in the envelope for auditability: a reviewer can see the discovery path
    without the record ever being usable as support for a fact.
    """
    return store.create(
        source_url=candidate.url,
        source_class=SOURCE_CANDIDATE_ONLY,
        retrieved_at=retrieved_at,
        claim_span=f"discovery: {candidate.origin} rank {candidate.rank}"
                   + (f" ({candidate.note})" if candidate.note else ""),
        extractor="discovery_v1",
    )

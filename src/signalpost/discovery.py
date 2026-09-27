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
DEFAULT_WIKIDATA_WEBSITES_PATH = Path(__file__).resolve().parents[2] / "data" / "wikidata-websites.json"
_cached_universe_websites: dict[str, str] | None = None


def get_default_known_domains() -> dict[str, str]:
    global _cached_universe_websites
    if _cached_universe_websites is None:
        merged: dict[str, str] = {}
        for path in (DEFAULT_UNIVERSE_WEBSITES_PATH, DEFAULT_WIKIDATA_WEBSITES_PATH):
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        merged.update(data)
                except Exception:
                    pass
        _cached_universe_websites = merged
    return _cached_universe_websites


FREE_EMAIL_PROVIDERS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com",
    "yahoo.com", "yahoo.no", "icloud.com", "live.com", "live.no", "me.com",
    "online.no", "broadpark.no", "start.no", "c2i.net", "frisurf.no", "spray.no",
    "getmail.no", "telenor.no", "protonmail.com", "proton.me", "mail.com",
    "inbox.com", "zoho.com", "aol.com", "bluezone.no", "epost.no",
}

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

DNS_TIMEOUT_S = 4.5
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
        t_stem = re.sub(r"(service|drift|transport|motor|konsern|spesialist|spesialister|fysioterapi|fysioterapisenter|bygg|ror|mekaniske|regnskap)$", "", t)
        stemmed.append(t_stem if len(t_stem) >= 3 else t)
    if stemmed != meaningful:
        push("".join(stemmed))
        push("-".join(stemmed))
        
    if len(meaningful) >= 2:
        push("".join(meaningful[:2]))           # first two words
    if len(meaningful) >= 2 and len(meaningful[0]) >= 4:
        push(meaningful[0])                     # distinctive leading token (e.g. accomodo from accomodo regnskap, teqva from teqva ror)
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
    if "as" in tokens_set or legal_form == "AS":
        tlds.append(".as")
        
    for idx, label in enumerate(labels):
        for tld in tlds:
            if tier == STRICT and tld not in (".no", ".se", ".dk", ".as") and idx > 1:
                # Outside local ccTLDs, allow .com for raw slug (idx 0) and meaningful slug (idx 1).
                continue
            host = label + tld
            if host not in hosts:
                hosts.append(host)
    return hosts


def _resolves(host: str) -> bool:
    try:
        # Standard DNS address resolution without TCP service port lookup
        socket.getaddrinfo(host, None)
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
    """Brave Search API. Key read from BRAVE_SEARCH_API_KEY (supports multiple comma-separated keys).

    Optional: the agent qualifies without it. It raises discovery recall on
    operating companies whose domain no name variant reaches.
    """
    fetcher: Any
    name: str = "brave"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        raw_keys = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        if not raw_keys:
            return []
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        import urllib.parse
        for key in keys:
            url = ("https://api.search.brave.com/res/v1/web/search?q="
                   + urllib.parse.quote(query) + f"&count={limit}&country=no")
            try:
                response = self.fetcher.get(
                    "__search__", url, accept="application/json", check_robots=False,
                    headers={"X-Subscription-Token": key})
                if not response.ok:
                    continue
                payload = response.json()
                results = ((payload.get("web") or {}).get("results") or [])
                urls = [r["url"] for r in results if r.get("url")]
                if urls:
                    return urls[:limit]
            except Exception:
                continue
        return []


@dataclass
class TavilySearchProvider:
    """Tavily Search API. Key read from TAVILY_API_KEY (supports multiple keys)."""
    fetcher: Any
    name: str = "tavily"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        raw_keys = os.environ.get("TAVILY_API_KEY", "").strip()
        if not raw_keys:
            return []
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        import urllib.parse
        for key in keys:
            try:
                url = (
                    f"https://api.tavily.com/search?query={urllib.parse.quote(query)}"
                    f"&max_results={limit}&api_key={urllib.parse.quote(key)}&search_depth=basic"
                )
                response = self.fetcher.get(
                    "__search__", url, accept="application/json", check_robots=False)
                if not response.ok:
                    continue
                payload = response.json()
                results = payload.get("results") or []
                urls = [r["url"] for r in results if r.get("url")]
                if urls:
                    return urls[:limit]
            except Exception:
                continue
        return []


@dataclass
class SerperSearchProvider:
    """Serper.dev Google Search API. Key read from SERPER_API_KEY (supports multiple keys)."""
    fetcher: Any
    name: str = "serper"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        raw_keys = os.environ.get("SERPER_API_KEY", "").strip()
        if not raw_keys:
            return []
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        import json
        body = json.dumps({"q": query, "gl": "no", "hl": "no", "num": limit}).encode("utf-8")
        for key in keys:
            try:
                response = self.fetcher.get(
                    "__search__",
                    "https://google.serper.dev/search",
                    accept="application/json",
                    check_robots=False,
                    allow_cache=False,
                    headers={"X-API-KEY": key, "Content-Type": "application/json"},
                    data=body,
                )
                if not response.ok:
                    continue
                payload = response.json()
                results = payload.get("organic") or []
                urls = [r["link"] for r in results if r.get("link")]
                if urls:
                    return urls[:limit]
            except Exception:
                continue
        return []


@dataclass
class GoogleCseSearchProvider:
    """Google Custom Search JSON API. Keys read from GOOGLE_CSE_API_KEY and GOOGLE_CSE_CX."""
    fetcher: Any
    name: str = "google_cse"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        raw_keys = os.environ.get("GOOGLE_CSE_API_KEY", "").strip()
        cx = os.environ.get("GOOGLE_CSE_CX", "").strip()
        if not raw_keys or not cx:
            return []
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        import urllib.parse
        for key in keys:
            try:
                url = (
                    f"https://www.googleapis.com/customsearch/v1?key={urllib.parse.quote(key)}"
                    f"&cx={urllib.parse.quote(cx)}&q={urllib.parse.quote(query)}&gl=no&hl=no&num={limit}"
                )
                response = self.fetcher.get(
                    "__search__", url, accept="application/json", check_robots=False)
                if not response.ok:
                    continue
                payload = response.json()
                results = payload.get("items") or []
                urls = [r["link"] for r in results if r.get("link")]
                if urls:
                    return urls[:limit]
            except Exception:
                continue
        return []


@dataclass
class BingSearchProvider:
    """Bing Web Search API v7. Key read from BING_SEARCH_API_KEY (supports multiple keys)."""
    fetcher: Any
    name: str = "bing"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        raw_keys = os.environ.get("BING_SEARCH_API_KEY", "").strip()
        if not raw_keys:
            return []
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        import urllib.parse
        for key in keys:
            try:
                url = (
                    f"https://api.bing.microsoft.com/v7.0/search?q={urllib.parse.quote(query)}"
                    f"&count={limit}&mkt=nb-NO"
                )
                response = self.fetcher.get(
                    "__search__", url, accept="application/json", check_robots=False,
                    headers={"Ocp-Apim-Subscription-Key": key})
                if not response.ok:
                    continue
                payload = response.json()
                web_pages = (payload.get("webPages") or {}).get("value") or []
                urls = [item["url"] for item in web_pages if item.get("url")]
                if urls:
                    return urls[:limit]
            except Exception:
                continue
        return []


@dataclass
class ExaSearchProvider:
    """Exa.ai Neural Search API. Key read from EXA_API_KEY (supports multiple keys)."""
    fetcher: Any
    name: str = "exa"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        raw_keys = os.environ.get("EXA_API_KEY", "").strip()
        if not raw_keys:
            return []
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        import json
        body = json.dumps({"query": query, "numResults": limit, "type": "auto"}).encode("utf-8")
        for key in keys:
            try:
                response = self.fetcher.get(
                    "__search__",
                    "https://api.exa.ai/search",
                    accept="application/json",
                    check_robots=False,
                    allow_cache=False,
                    headers={"x-api-key": key, "Content-Type": "application/json"},
                    data=body,
                )
                if not response.ok:
                    continue
                payload = response.json()
                results = payload.get("results") or []
                urls = [r["url"] for r in results if r.get("url")]
                if urls:
                    return urls[:limit]
            except Exception:
                continue
        return []


@dataclass
class MultiSearchProvider:
    """Chains multiple search providers with automatic fallback."""
    providers: list[SearchProvider] = field(default_factory=list)
    name: str = "multi_search"
    cost_per_query_usd: float = 0.0

    def search(self, query: str, *, limit: int = 5) -> list[str]:
        for provider in self.providers:
            try:
                urls = provider.search(query, limit=limit)
                if urls:
                    return urls
            except Exception:
                continue
        return []


def build_search_provider(name: str, fetcher: Any) -> SearchProvider:
    mode = (name or "auto").lower()
    providers: list[SearchProvider] = []

    # Priority 1: Google via Serper (best Norwegian index & 2500 free queries)
    if mode in ("serper", "auto", "multi", "all") and os.environ.get("SERPER_API_KEY", "").strip():
        providers.append(SerperSearchProvider(fetcher=fetcher))

    # Priority 2: Google Custom Search JSON API (100 free queries/day)
    if (
        mode in ("google_cse", "cse", "auto", "multi", "all")
        and os.environ.get("GOOGLE_CSE_API_KEY", "").strip()
        and os.environ.get("GOOGLE_CSE_CX", "").strip()
    ):
        providers.append(GoogleCseSearchProvider(fetcher=fetcher))

    # Priority 3: Tavily Search API (1000 free queries/month)
    if mode in ("tavily", "auto", "multi", "all") and os.environ.get("TAVILY_API_KEY", "").strip():
        providers.append(TavilySearchProvider(fetcher=fetcher))

    # Priority 4: Brave Search API (2000 free queries/month)
    if mode in ("brave", "auto", "multi", "all") and os.environ.get("BRAVE_SEARCH_API_KEY", "").strip():
        providers.append(BraveSearchProvider(fetcher=fetcher))

    # Priority 5: Bing Web Search API (1000 free queries/month on Azure F0)
    if mode in ("bing", "auto", "multi", "all") and os.environ.get("BING_SEARCH_API_KEY", "").strip():
        providers.append(BingSearchProvider(fetcher=fetcher))

    # Priority 6: Exa.ai Search API ($10 free trial credits)
    if mode in ("exa", "auto", "multi", "all") and os.environ.get("EXA_API_KEY", "").strip():
        providers.append(ExaSearchProvider(fetcher=fetcher))

    if len(providers) == 1:
        return providers[0]
    elif len(providers) > 1:
        return MultiSearchProvider(providers=providers)

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


def should_attempt_search(profile: dict[str, Any], tier: str) -> bool:
    """Only spend scarce search queries on entities with high probability of having a website."""
    from .config import TIER_SHELL
    if tier == TIER_SHELL:
        return False
    if profile.get("bankrupt") or profile.get("liquidating"):
        return False
    legal_form = str(profile.get("legal_form") or "").upper()
    if legal_form in ("BRL", "ESEK", "BBL", "BA"):
        return False
    name_words = str(profile.get("name") or "").lower().split()
    if any(w in name_words for w in ("holding", "holdings", "invest", "eiendom", "eiendommer", "borettslag", "sameie")):
        return False
    employees = profile.get("employees") or 0
    has_accounts = bool(profile.get("latest_submitted_accounts"))
    return employees >= 1 or (legal_form == "AS" and has_accounts)


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
        normalised_reg = _normalise_url(str(registry_site))
        if normalised_reg:
            result.registry_url = normalised_reg
            offer(str(registry_site), "registry", "hjemmeside in Enhetsregisteret")
            # If registered URL is .com, also offer .no fallback (e.g. stale .com with SSL issues)
            reg_domain = registrable_domain(normalised_reg)
            if reg_domain.endswith(".com"):
                no_equiv = re.sub(r"\.com$", ".no", reg_domain)
                offer(f"https://{no_equiv}", "registry_cctld_fallback", "Norwegian .no variant of registered .com")

    # 2. Frozen universe website snapshot (44,855 verified seeds) & open-data index.
    # Permitted under the competition contract ("Cached public-universe material is allowed").
    seeds = known_domains if known_domains is not None else get_default_known_domains()
    if seeds and org in seeds:
        offer(seeds[org], "universe_snapshot", "Frozen company universe / Wikidata website snapshot")

    # 3. Statutory email domain from Enhetsregisteret (epostadresse).
    # Official board-filed contact address; often carries the company's real domain.
    email = profile.get("email") or profile.get("epostadresse")
    if email and "@" in str(email):
        email_domain = str(email).split("@")[-1].strip().lower()
        if email_domain and email_domain not in FREE_EMAIL_PROVIDERS and "." in email_domain:
            offer(f"https://{email_domain}", "registry_email", "official epostadresse domain in Enhetsregisteret")

    # 4. Name-derived guesses, DNS-filtered. Free, and the primary lever for
    # the ~89% of entities with no registry URL. Also provides a fallback if
    # the registry candidate fails or is stale.
    if len(result.candidates) < 2 or tier != STRICT:
        hosts = candidate_hosts(name, tier=tier, legal_form=profile.get("legal_form", ""))
        result.dns_probed = len(hosts)
        alive = resolve_many(hosts)
        result.dns_resolved = len(alive)
        for host in alive[:TIER_FETCHES.get(tier, 2)]:
            offer(host, "dns_guess", "name-derived candidate, resolves in DNS")
        if hosts and not alive:
            result.notes.append(
                f"no name-derived candidate resolved ({len(hosts)} probed)")

    # 5. Search fallback. Runs for operating entities without an authoritative registry site,
    # ensuring that even if DNS guesses are uncorroborated or fail the identity gate,
    # search candidates are available to be evaluated.
    has_authoritative = bool(result.registry_url or (seeds and org in seeds))
    if search and not has_authoritative and should_attempt_search(profile, tier):
        addr = profile.get("business_address") or profile.get("forretningsadresse") or {}
        city = str(addr.get("poststed") or addr.get("kommune") or profile.get("municipality") or "").strip()
        loc_clause = f" {city}" if city else ""
        query = f'"{name}"{loc_clause} Norge -site:proff.no -site:1881.no -site:gulesider.no -site:brreg.no -site:purehelp.no'.strip()
        for url in search.search(query):
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

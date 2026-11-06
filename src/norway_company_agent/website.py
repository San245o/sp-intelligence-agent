from __future__ import annotations

import json
import copy
import threading
import hashlib
import os
import tempfile
from pathlib import Path
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence
from .source_store import capture_source
from .network_meter import RequestLimitExceeded

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "kontakt", "contact", "karriere", "career", "stillinger", "jobs", "jobb", "om-oss", "om_oss", "about", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter",
)

# The package default writes a suffix-list cache beside site-packages in some
# Windows installs. Use its bundled snapshot so domain checks work in a
# read-only installation and never trigger an uncounted network fetch.
DOMAIN_EXTRACTOR = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=())


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = DOMAIN_EXTRACTOR(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


_ROBOTS_CACHE: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None, bool]] = {}
_ROBOTS_LOCK = threading.RLock()
_ROBOTS_ORIGIN_LOCKS: dict[str, threading.Lock] = {}


def _robots_check(url: str, timeout: float) -> tuple[bool, int]:
    """Return permission and actual robots attempts; serialize first reads per origin."""
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    with _ROBOTS_LOCK:
        origin_lock = _ROBOTS_ORIGIN_LOCKS.setdefault(robots_url, threading.Lock())
    with origin_lock:
        cached = _ROBOTS_CACHE.get(robots_url)
        if cached and cached[0] > time.monotonic():
            return cached[1].can_fetch(USER_AGENT, url) if cached[1] else cached[2], 0
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        allowed = False
        ttl = 60.0
        try:
            request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
            with SAFE_OPENER.open(request, timeout=timeout) as response:
                body = response.read(512_001)
            if len(body) > 512_000:
                raise ValueError("robots.txt exceeded size limit")
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            allowed = parser.can_fetch(USER_AGENT, url)
            ttl = 300.0
        except urllib.error.HTTPError as exc:
            parser = None
            allowed = exc.code in {404, 410}
        except Exception:
            parser = None
        _ROBOTS_CACHE[robots_url] = (time.monotonic() + ttl, parser, allowed)
        return allowed, 1


def _robots_allowed(url: str, timeout: float) -> bool:
    return _robots_check(url, timeout)[0]


def robots_sitemaps(url: str) -> list[str]:
    parsed = urllib.parse.urlparse(url)
    key = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    with _ROBOTS_LOCK:
        cached = _ROBOTS_CACHE.get(key)
        return list(cached[1].site_maps() or []) if cached and cached[0] > time.monotonic() and cached[1] else []


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if term in haystack), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates[clean] = min(rank, candidates.get(clean, rank))
    # Spend the bounded crawl on different evidence-bearing sections before
    # fetching several translations or branches of the same contact page.
    ordered = sorted(candidates, key=lambda url: (candidates[url], url))
    def category(url: str) -> str:
        path = urllib.parse.urlparse(url).path.casefold()
        if any(term in path for term in ("kontakt", "contact")):
            return "contact"
        if any(term in path for term in ("career", "karriere", "stilling", "jobs", "jobb")):
            return "careers"
        if any(term in path for term in ("news", "press", "aktuelt", "nyheter")):
            return "news"
        return "identity"
    first, remaining, groups = [], [], set()
    for url in ordered:
        group = category(url)
        if group in groups:
            remaining.append(url)
        else:
            groups.add(group)
            first.append(url)
    return (first + remaining)[:limit]


def _role_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Recover bounded same-site role leads from a careers page, not arbitrary links."""
    parsed = urllib.parse.urlparse(base_url)
    if not re.search(r"(?:career|karriere|stilling|jobs|jobb)", parsed.path, re.I):
        return []
    links = []
    for anchor in soup.select("a[href]"):
        url = urllib.parse.urljoin(base_url, str(anchor.get("href") or ""))
        target = urllib.parse.urlparse(url)
        if target.scheme not in {"http", "https"} or target.netloc.casefold() != parsed.netloc.casefold():
            continue
        path = target.path.rstrip("/")
        if not re.search(r"/(?:careers?|karriere|stillinger|jobs|jobb)(?:/|[-_]).+", path, re.I):
            continue
        if path.lower().endswith((".pdf", ".jpg", ".png", ".xml")):
            continue
        clean = urllib.parse.urlunparse(target._replace(query="", fragment=""))
        if clean.rstrip("/") != base_url.rstrip("/") and clean not in links:
            links.append(clean)
    return links[:2]


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    allowed, robots_requests = _robots_check(url, timeout)
    if not allowed:
        return None, [], robots_requests, 0, 0, "robots.txt disallows page or could not be verified"
    request_count = robots_requests + 1
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], request_count, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], request_count, len(raw), elapsed, "redirected outside registered domain"
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_structured = extruct.extract(page_html, base_url=final_url, syntaxes=["json-ld"])
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        page = {
            **capture_source(raw),
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "identity_text_excerpt": _visible_text(page_html),
            "ownership_text_excerpt": _ownership_text(page_soup),
            "heading": page_soup.h1.get_text(" ", strip=True) if page_soup.h1 else "",
            "main_text_excerpt": page_text[:5000],
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "role_links": _role_links(final_url, page_soup),
            "structured_organisations": _jsonld_organisations(page_structured),
        }
        return page, _social_links(final_url, page_soup), request_count, len(raw), elapsed, None
    except Exception as exc:
        return None, [], request_count, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def _article_document(node):
    if isinstance(node, dict):
        kind = node.get('@type') or []
        types = kind if isinstance(kind, list) else [kind]
        return bool(set(str(t) for t in types) & {'Article', 'NewsArticle', 'BlogPosting'}) or any(_article_document(v) for v in node.values())
    return isinstance(node, list) and any(_article_document(v) for v in node)


def _ownership_text(soup: BeautifulSoup) -> str:
    regions = soup.select('body > header, body > footer, footer, address, [role="banner"], [role="contentinfo"]')
    return ' '.join(node.get_text(' ', strip=True) for node in regions)[:12000]


def _visible_text(html: str, limit: int = 12000) -> str:
    visible = BeautifulSoup(html, "lxml")
    for node in visible.select("script,style,noscript,svg"):
        node.decompose()
    return " ".join(visible.stripped_strings)[:limit]


def _fetch_website(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000, max_secondary_pages: int = 4) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        # DNS failure is an unavailable lead, not a robots or safety-policy block.
        status = "source_error" if str(exc) == "Hostname did not resolve" else "blocked"
        return evidence("website", status, "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    allowed, robots_requests = _robots_check(normalized, timeout)
    request_count = robots_requests + 1
    if not allowed:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent"), {"requests": robots_requests, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": request_count, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": request_count, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        homepage_content_sha256 = __import__("hashlib").sha256(raw).hexdigest()
        homepage_social = [
            {**item, "discovery_source": "verified_site_link", "evidence_url": final_url, "evidence_sha256": homepage_content_sha256}
            for item in _social_links(final_url, soup)
        ]
        value = {
            **capture_source(raw),
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "identity_text_excerpt": _visible_text(html),
            "ownership_text_excerpt": _ownership_text(soup),
            "heading": soup.h1.get_text(" ", strip=True) if soup.h1 else "",
            "main_text_excerpt": text[:5000],
            "social_links": homepage_social,
            "feed_links": [
                urllib.parse.urljoin(final_url, str(node.get("href") or ""))
                for node in soup.select('link[rel~="alternate"][href]')
                if any(token in str(node.get("type") or "").casefold() for token in ("rss", "atom", "xml"))
            ][:4],
            "structured_organisations": _jsonld_organisations(structured),
            "article_document": _article_document(structured.get('json-ld', [])),
            "content_sha256": homepage_content_sha256,
            "extraction_state": _extraction_state(text, soup),
        }
        pages = [{**capture_source(raw), "url": final_url, "title": title[:500], "identity_text_excerpt": value["identity_text_excerpt"], "main_text_excerpt": text[:5000], "content_sha256": value["content_sha256"]}]
        social = value["social_links"]
        crawl_errors = []
        requests = request_count
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = value["registered_domain"]
        pending = _priority_links(final_url, soup, limit=max_secondary_pages)
        entry = urllib.parse.urlparse(final_url)
        if entry.path.strip("/") and not re.fullmatch(r"(?:no|en|nb|nn|index\.html?)", entry.path.strip("/"), re.I):
            value["ownership_homepage_required"] = True
            pending.insert(0, urllib.parse.urlunparse(entry._replace(path="/", params="", query="", fragment="")))
        visited = {final_url.rstrip("/")}
        secondary_attempts = 0
        while pending and secondary_attempts < max_secondary_pages:
            page_url = pending.pop(0)
            if page_url.rstrip("/") in visited:
                continue
            visited.add(page_url.rstrip("/"))
            secondary_attempts += 1
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=min(max_bytes, 1_000_000),
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                pages.append(page)
                if value.get("ownership_homepage_required") and not urllib.parse.urlparse(page_url).path.strip("/"):
                    value["ownership_homepage"] = page
                # Keep the contact/about evidence already selected, then spend
                # the last slot on a real role page ahead of a general news index.
                for role_url in reversed(page.get("role_links") or []):
                    if role_url.rstrip("/") not in visited and role_url not in pending:
                        pending.insert(min(1, len(pending)), role_url)
                social.extend(
                    {**item, "discovery_source": "verified_site_link", "evidence_url": page.get("url"), "evidence_sha256": page.get("content_sha256")}
                    for item in page_social
                )
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())
        value["crawl_errors"] = crawl_errors
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": request_count, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://"):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = _fetch_website("http://" + supplied_url, timeout=timeout, max_bytes=max_bytes, max_secondary_pages=max_secondary_pages)
            metrics["requests"] += request_count
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": request_count, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": request_count, "bytes": 0, "latencies_ms": [elapsed]}


_FETCH_CACHE: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_FETCH_LOCK = threading.Lock()
_FETCH_URL_LOCKS: dict[tuple[str, int, int], threading.Lock] = {}
FETCH_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "web-fetch-cache"
READ_DISK_CACHE = True


def _disk_record(key: tuple, record: dict | None = None, ttl: float = 0) -> dict | None:
    if FETCH_CACHE_DIR is None:
        return None
    path = FETCH_CACHE_DIR / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + ".json")
    try:
        if record is None:
            if not READ_DISK_CACHE:
                return None
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("version") != 3 or entry.get("expires", 0) <= time.time():
                return None
            result = entry["record"]
            if result.get("status") == "available":
                value = result.get("value") or {}
                source = Path(value.get("snapshot_path") or "")
                if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != value.get("snapshot_sha256"):
                    return None
            return result
        # Captures are mandatory for reusable positive entries.
        if record.get("status") == "available" and not (record.get("value") or {}).get("snapshot_path"):
            return None
        FETCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="fetch-", dir=FETCH_CACHE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 3, "expires": time.time() + ttl, "record": record}, handle, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, ValueError, KeyError, TypeError):
        # Cache storage is optional; an unusable entry triggers real acquisition.
        return None
    return None


def fetch_website(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000, max_secondary_pages: int = 4) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reuse an immutable acquisition result; rerun company identity on every copy.

    Source retrieval times survive cache hits. Failures expire quickly so a
    temporary source outage does not become a permanent negative record.
    """
    normalized = normalize_homepage(url)
    if not normalized:
        return _fetch_website(url, timeout=timeout, max_bytes=max_bytes, max_secondary_pages=max_secondary_pages)
    parsed = urllib.parse.urlparse(normalized)
    key = (urllib.parse.urlunparse(parsed._replace(netloc=parsed.netloc.lower())), max_bytes, max_secondary_pages)
    with _FETCH_LOCK:
        url_lock = _FETCH_URL_LOCKS.setdefault(key, threading.Lock())
    with url_lock:
        cached = _FETCH_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1]), {"requests": 0, "bytes": 0, "latencies_ms": [], "cache_hit": True}
        disk = _disk_record(key)
        if disk is not None:
            return disk, {"requests": 0, "bytes": 0, "latencies_ms": [], "cache_hit": True, "cache_source": "disk"}
        try:
            record, metrics = _fetch_website(url, timeout=timeout, max_bytes=max_bytes, max_secondary_pages=max_secondary_pages)
        except RequestLimitExceeded:
            return evidence("website", "not_fetched", "registry_linked_company_website", normalized, note="request_budget_exhausted"), {"requests": 0, "bytes": 0, "latencies_ms": [], "budget_exhausted": True}
        ttl = 3600 if record.get("status") == "available" else 30
        _disk_record(key, record, ttl)
        with _FETCH_LOCK:
            if len(_FETCH_CACHE) >= 2048:
                oldest = min(_FETCH_CACHE, key=lambda candidate: _FETCH_CACHE[candidate][0])
                _FETCH_CACHE.pop(oldest, None)
            _FETCH_CACHE[key] = (time.monotonic() + ttl, copy.deepcopy(record))
        return record, metrics

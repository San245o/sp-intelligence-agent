"""Own-site crawler: fetch a small, high-value set of pages from one domain.

Once the identity gate has accepted a domain as the company's own, this walks a
budget-capped set of pages that carry the facts the rubric rewards — contact,
leadership, locations, careers, news. It is deliberately shallow: the run has
~20 requests per company on average, so a site gets `MAX_PAGES_PER_SITE` at most
and priority paths are tried before any discovered link.

Page selection order:
  1. the accepted home page (already fetched by the identity gate; reused free)
  2. sitemap.xml entries whose path looks high-value
  3. PRIORITY_PATHS that were not already seen
  4. same-host links harvested from the home page, ranked by anchor/path signal

Every fetch goes through the shared Fetcher, so robots.txt, per-host politeness
and the request budget all apply uniformly.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .config import MAX_PAGES_PER_SITE, PRIORITY_PATHS
from .http import Fetcher, Response

#: Anchor/path keywords that mark a link as worth a scarce request.
VALUE_KEYWORDS = (
    "om-oss", "about", "kontakt", "contact", "ledelse", "leadership", "team",
    "ansatte", "people", "styre", "board", "karriere", "career", "jobb", "job",
    "ledige", "stilling", "nyheter", "news", "aktuelt", "presse", "press",
    "lokasjon", "location", "avdeling", "office", "investor", "impressum",
)

#: Paths that never carry entity facts; skipped even if linked prominently.
SKIP_KEYWORDS = (
    "/cart", "/handlekurv", "/login", "/logg-inn", "/wp-admin", "/wp-login",
    "/tag/", "/category/", "/produkt/", "/product/", "/cookie", "/personvern",
    ".pdf", ".jpg", ".png", ".zip", ".mp4", "/feed", "/rss", "?add-to-cart",
)


@dataclass
class Page:
    url: str
    final_url: str
    status: int | None
    html: str
    content_sha256: str
    retrieved_at: str
    path_kind: str            # home | sitemap | priority | link


@dataclass
class SiteCrawl:
    domain: str
    pages: list[Page] = field(default_factory=list)
    fetched: int = 0
    skipped_robots: int = 0
    notes: list[str] = field(default_factory=list)

    def html_by_kind(self) -> dict[str, list[Page]]:
        out: dict[str, list[Page]] = {}
        for page in self.pages:
            out.setdefault(page.path_kind, []).append(page)
        return out


def _same_host(base: str, url: str) -> bool:
    try:
        b = urllib.parse.urlparse(base).hostname or ""
        u = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    b = b[4:] if b.startswith("www.") else b
    u = u[4:] if u.startswith("www.") else u
    return bool(b) and b == u


def _looks_valuable(url: str) -> bool:
    low = url.lower()
    if any(skip in low for skip in SKIP_KEYWORDS):
        return False
    return any(keyword in low for keyword in VALUE_KEYWORDS)


def _harvest_links(base_url: str, html: str) -> list[str]:
    """Same-host links from anchor tags, valuable ones first, deduplicated."""
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                             html, re.I | re.S):
        href, anchor = match.group(1), match.group(2)
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        absolute = absolute.split("#")[0]
        if not absolute.startswith("http") or not _same_host(base_url, absolute):
            continue
        host = absolute.lower()
        if host in seen:
            continue
        seen.add(host)
        anchor_text = re.sub(r"<[^>]+>", " ", anchor).lower()
        score = 0
        if _looks_valuable(absolute):
            score += 2
        if any(k in anchor_text for k in VALUE_KEYWORDS):
            score += 1
        if any(skip in absolute.lower() for skip in SKIP_KEYWORDS):
            score -= 5
        found.append((score, absolute))
    found.sort(key=lambda t: -t[0])
    return [url for score, url in found if score > 0]


def _sitemap_urls(fetcher: Fetcher, org: str, origin: str) -> list[str]:
    """Valuable URLs from sitemap.xml, if present. One request, best effort."""
    response = fetcher.get(org, origin + "/sitemap.xml", accept="application/xml")
    if not response.ok or "xml" not in (response.content_type or "").lower():
        return []
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", response.text, re.I)
    # A sitemap index points at more sitemaps; follow one level only, cheaply.
    valuable = [u for u in urls if _looks_valuable(u)]
    return valuable[:MAX_PAGES_PER_SITE]


def crawl_site(
    fetcher: Fetcher,
    org: str,
    domain: str,
    *,
    home: Response,
    max_pages: int = MAX_PAGES_PER_SITE,
) -> SiteCrawl:
    """Fetch up to `max_pages` high-value pages from an accepted domain.

    `home` is the already-fetched, identity-accepted home page; it is reused
    without a second request.
    """
    crawl = SiteCrawl(domain=domain)
    origin = f"https://{domain}"
    seen_urls: set[str] = set()

    def record(response: Response, kind: str) -> None:
        key = (response.final_url or response.url).split("#")[0].lower()
        if key in seen_urls:
            return
        seen_urls.add(key)
        crawl.pages.append(Page(
            url=response.url, final_url=response.final_url, status=response.status,
            html=response.text if response.is_html else "",
            content_sha256=response.content_sha256,
            retrieved_at=response.retrieved_at, path_kind=kind))

    # 1. Home page, reused.
    if home.ok:
        record(home, "home")

    # 2. Build an ordered queue of additional URLs.
    queue: list[tuple[str, str]] = []
    for url in _sitemap_urls(fetcher, org, origin):
        queue.append((url, "sitemap"))
    for path in PRIORITY_PATHS:
        if path == "/":
            continue
        queue.append((origin + path, "priority"))
    if home.ok and home.is_html:
        for url in _harvest_links(home.final_url or origin, home.text):
            queue.append((url, "link"))

    # 3. Walk the queue until the page cap or the budget stops us.
    for url, kind in queue:
        if len(crawl.pages) >= max_pages:
            crawl.notes.append(f"page cap reached ({max_pages})")
            break
        clean = url.split("#")[0]
        if clean.lower() in seen_urls or not _same_host(origin, clean):
            continue
        response = fetcher.get(org, clean)
        crawl.fetched += 1
        if response.blocked_by_robots:
            crawl.skipped_robots += 1
            continue
        if response.error == "budget_exhausted":
            crawl.notes.append("request budget exhausted mid-crawl")
            break
        if response.ok and response.is_html:
            record(response, kind)

    return crawl

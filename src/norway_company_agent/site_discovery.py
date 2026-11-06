from __future__ import annotations

import hashlib
import gzip
import io
import json
from pathlib import Path
from datetime import date, datetime, timezone
import email.utils
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable

from bs4 import BeautifulSoup
import trafilatura
from .source_store import capture_source
from .news_quality import publication_eligibility

from .website import SAFE_OPENER, USER_AGENT, _registered_domain, _robots_check, assert_public_url, robots_sitemaps

NEWS_PATH = re.compile(r"/(?:[^/]+/)*(?:news|newsroom|nyhet(?:er)?|aktuelt|press(?:room)?|presse|pressemeldinger|artikler|articles|blogg?|innsikt|insights?)(?:/|[-_])", re.I)
SKIP_PATH = re.compile(r"/(?:tag|category|author|page|feed|sitemap)(?:/|$)", re.I)
MAX_XML_BYTES = 8_000_000
MAX_PAGE_BYTES = 4_000_000


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _children_text(node: ET.Element, names: set[str]) -> str:
    for child in node.iter():
        if _local_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def _feed_dates(node: ET.Element) -> tuple[str, str, str]:
    """Keep publication dates distinct from feed modification timestamps."""
    published_names = ("pubdate", "published", "date", "issued")
    updated_names = ("updated", "modified", "lastmod")
    for name in published_names:
        value = _children_text(node, {name})
        normalized = _date_only(value) if value else ""
        if normalized:
            return normalized, "published", name
    for name in updated_names:
        value = _children_text(node, {name})
        normalized = _date_only(value) if value else ""
        if normalized:
            return normalized, "modified", name
    return "", "unknown", ""


def _date_only(raw: str) -> str:
    raw = raw.strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if match:
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            return ""
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.date().isoformat()
    except (TypeError, ValueError, IndexError):
        return ""


def _page_publication_date(soup: BeautifulSoup, url: str) -> tuple[str, str]:
    for selector, attribute in (
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="datePublished"]', "content"),
        ('meta[itemprop="datePublished"]', "content"),
        ('time[itemprop="datePublished"][datetime]', "datetime"),
        ('time.published[datetime]', "datetime"),
        ('time.entry-date:not(.updated)[datetime]', "datetime"),
    ):
        marker = soup.select_one(selector)
        value = _date_only(str(marker.get(attribute) or "")) if marker else ""
        if value:
            return value, f"page:{selector}"
    articles = []
    def walk(node):
        if isinstance(node, dict):
            kind = node.get("@type")
            types = kind if isinstance(kind, list) else [kind]
            if any(item in {"Article", "NewsArticle", "BlogPosting", "Report"} for item in types):
                articles.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text() or "{}"))
        except (ValueError, TypeError):
            continue
    for article in articles:
        declared = article.get("url") or article.get("mainEntityOfPage") or article.get("@id")
        if isinstance(declared, dict):
            declared = declared.get("@id")
        if declared and str(declared).split("#")[0].rstrip("/") != url.split("#")[0].rstrip("/"):
            continue
        if not declared and len(articles) != 1:
            continue
        value = _date_only(str(article.get("datePublished") or ""))
        if value:
            return value, "page:Article.datePublished"
    # A date in the article's own heading is a publication marker. Dates in
    # related-story cards, body text and copyright footers are not.
    header = soup.select_one("article > header, article header, main header")
    if header and header.select_one("h1"):
        text = header.get_text(" ", strip=True)
        months = {name: index for index, names in enumerate((
            "januar january", "februar february", "mars march", "april", "mai may",
            "juni june", "juli july", "august", "september", "oktober october",
            "november", "desember december"), 1) for name in names.split()}
        found = list(re.finditer(r"\b(\d{1,2})\.?\s+([A-Za-zæøå]+)\s+(20\d{2})\b", text, re.I))
        if len(found) == 1 and not re.search(r"oppdatert|modified|updated|revidert", text, re.I):
            marker = found[0]
            try:
                value = date(int(marker[3]), months[marker[2].casefold()], int(marker[1])).isoformat()
                return value, "page:article_header_visible_date"
            except (ValueError, KeyError):
                pass
    return "", ""


def _article_candidate(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.casefold()
    return bool(NEWS_PATH.search(path) and not SKIP_PATH.search(path)
                and not path.endswith((".xml", ".xml.gz", ".rss", ".atom", ".json"))
                and path.rstrip("/").rsplit("/", 1)[-1] not in {"news", "nyheter", "aktuelt", "press", "presse", "blog", "blogg", "articles", "artikler"})


def _same_site(url: str, domain: str) -> bool:
    try:
        return urllib.parse.urlparse(url).scheme in {"http", "https"} and _registered_domain(url) == domain
    except Exception:
        return False


def _read(url: str, domain: str, limit: int, timeout: float) -> tuple[bytes | None, dict[str, Any]]:
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": [], "outcome": "not_attempted", "reason": None}
    if not _same_site(url, domain):
        metrics.update(outcome="candidate_rejected", reason="URL is outside the verified company domain.")
        return None, metrics
    try:
        assert_public_url(url)
        # robots.txt is a separate request when the website crawler has not cached it.
        allowed, robots_requests = _robots_check(url, timeout)
        metrics["requests"] += robots_requests
        if not allowed:
            metrics.update(outcome="blocked", reason="Robots policy disallows this URL or could not be verified.")
            return None, metrics
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,application/rss+xml,application/atom+xml,text/html,*/*;q=0.3"})
        started = time.monotonic()
        metrics["requests"] += 1
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
            final_url = response.geturl()
            content_type = response.headers.get("content-type", "").casefold()
        metrics["bytes"] = len(body)
        metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
        if len(body) > limit or not _same_site(final_url, domain):
            metrics.update(outcome="blocked", reason="Response exceeded the size limit or redirected outside the verified domain.")
            return None, metrics
        if body.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                body = compressed.read(limit + 1)
            if len(body) > limit:
                metrics.update(outcome="blocked", reason="Decompressed response exceeded the size limit.")
                return None, metrics
        if "html" not in content_type and b"<" not in body[:1024]:
            metrics.update(outcome="parse_failure", reason=f"Unexpected response content type: {content_type or 'unknown'}.")
            return None, metrics
        metrics["outcome"] = "fetched"
        metrics.update(capture_source(body))
        return body, metrics
    except urllib.error.HTTPError as exc:
        metrics.update(outcome="not_found" if exc.code in {404, 410} else "provider_error", reason=f"HTTP {exc.code}")
        return None, metrics
    except urllib.error.URLError as exc:
        metrics.update(outcome="provider_error", reason=f"{type(exc.reason).__name__}: {str(exc.reason)[:160]}")
        return None, metrics
    except Exception:
        metrics.update(outcome="provider_error", reason="Request failed before a usable response was captured.")
        return None, metrics


def discover_site_activity(
    website_value: dict[str, Any],
    *,
    reserve: Callable[[int], bool],
    charge: Callable[[dict[str, Any]], None],
    max_articles: int = 12,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Read a verified company's own feed/sitemap and capture a few dated article pages."""
    homepage = str(website_value.get("final_url") or "")
    domain = str(website_value.get("registered_domain") or "")
    if not homepage or not domain:
        return {"status": "not_available", "reason": "verified homepage URL is missing", "articles": []}
    origin = f"{urllib.parse.urlparse(homepage).scheme}://{urllib.parse.urlparse(homepage).netloc}"
    candidates = list(website_value.get("feed_links") or [])[:2]
    if not candidates:
        candidates = [origin + "/feed/", origin + "/rss.xml"]
    items: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []
    source_attempts: list[dict[str, Any]] = []
    requests = byte_count = 0
    latencies: list[int] = []
    budget_exhausted = False
    acquisition_times = {}

    def read_limited(url: str, limit: int) -> bytes | None:
        nonlocal requests, byte_count, budget_exhausted
        # Leave enough allowance for robots.txt and the actual document.
        if not reserve(2):
            budget_exhausted = True
            source_attempts.append({"url": url, "outcome": "budget_exhausted", "reason": "Could not reserve robots and document requests.", "requests": 0})
            return None
        body, met = _read(url, domain, limit, timeout)
        charge(met)
        requests += met["requests"]
        byte_count += met["bytes"]
        latencies.extend(met["latencies_ms"])
        source_attempts.append({"url": url, "outcome": met.get("outcome"), "reason": met.get("reason"), "requests": met["requests"], "bytes": met["bytes"]})
        acquisition_times[url] = met.get('retrieved_at') or datetime.now(timezone.utc).isoformat()
        return body

    for feed_url in candidates:
        body = read_limited(feed_url, MAX_XML_BYTES)
        if not body:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            source_attempts.append({"url": feed_url, "outcome": "parse_failure", "reason": "Feed response was not valid XML.", "requests": 0})
            continue
        for node in root.iter():
            if _local_name(node.tag) not in {"item", "entry"}:
                continue
            title = _children_text(node, {"title"})
            link = _children_text(node, {"link"})
            if not link:
                for child in node:
                    if _local_name(child.tag) == "link":
                        link = child.attrib.get("href", "")
                        if child.attrib.get("rel", "alternate") == "alternate":
                            break
            date_value, date_kind, date_source = _feed_dates(node)
            if title and link and _same_site(urllib.parse.urljoin(feed_url, link), domain):
                items.append({
                    "url": urllib.parse.urljoin(feed_url, link), "title": title[:300],
                    "published": date_value if date_kind == "published" else "",
                    "modified": date_value if date_kind == "modified" else "",
                    "date_kind": date_kind, "date_source": f"feed:{date_source}" if date_source else "",
                })
        if items:
            sources.append({"kind": "feed", "url": feed_url, "content_sha256": hashlib.sha256(body).hexdigest()})
            break

    # A dated company feed already supplies stronger publication evidence than
    # sitemap lastmod, so avoid the sitemap request when it has enough entries.
    declared = [url for url in robots_sitemaps(homepage) if _same_site(url, domain)]
    sitemap_queue = [(url, 0) for url in (declared[:2] or [origin + "/sitemap.xml"])]
    sitemap_seen = set()
    while sitemap_queue and len(sitemap_seen) < 6 and sum(bool(item["published"]) for item in items) < max_articles:
        sitemap_url, depth = sitemap_queue.pop(0)
        if sitemap_url in sitemap_seen:
            continue
        sitemap_seen.add(sitemap_url)
        sitemap = read_limited(sitemap_url, MAX_XML_BYTES)
        if not sitemap:
            continue
        try:
            root = ET.fromstring(sitemap)
            locs = [node.text.strip() for node in root.iter() if _local_name(node.tag) == "loc" and node.text]
            sitemap_dates: dict[str, str] = {}
            for node in root.iter():
                if _local_name(node.tag) == "url":
                    loc = _children_text(node, {"loc"})
                    lastmod = _date_only(_children_text(node, {"lastmod"}))
                    if loc and lastmod:
                        sitemap_dates[loc] = lastmod
            nested = [loc for loc in locs if _same_site(loc, domain)] if _local_name(root.tag) == "sitemapindex" else []
            article_urls = [loc for loc in locs if _same_site(loc, domain) and _article_candidate(loc)]
            news_map = re.compile(r"news|nyhet|post|artik|blog|press|aktuelt", re.I)
            nested.sort(key=lambda candidate: not bool(news_map.search(candidate)))
            if depth < 3:
                sitemap_queue.extend((loc, depth + 1) for loc in nested if loc not in sitemap_seen
                                     and not re.search(r"product|tag|category|author|attachment", loc, re.I))
            sources.append({"kind": "sitemap", "url": sitemap_url, "content_sha256": hashlib.sha256(sitemap).hexdigest()})
            known = {item["url"] for item in items}
            for url in article_urls:
                if url not in known:
                    items.append({
                        "url": url, "title": "", "published": "", "modified": sitemap_dates.get(url, ""),
                        "date_kind": "modified" if sitemap_dates.get(url) else "unknown",
                        "date_source": "sitemap:lastmod" if sitemap_dates.get(url) else "",
                    })
                    known.add(url)
        except ET.ParseError:
            source_attempts.append({"url": sitemap_url, "outcome": "parse_failure", "reason": "Sitemap response was not valid XML.", "requests": 0})

    # Retained article pages are evidence too. Reprocess their captured bytes rather
    # than excluding them merely because the homepage crawler visited them first.
    retained = {str(page.get("url") or "").rstrip("/"): page for page in website_value.get("pages") or []}
    retained[homepage.rstrip("/")] = {**retained.get(homepage.rstrip("/"), {}), **website_value}
    known = {item["url"].rstrip("/") for item in items}
    for url, page in retained.items():
        if _article_candidate(url) and url not in known:
            items.append({"url": url, "title": page.get("title", ""), "published": "", "modified": "", "date_kind": "unknown", "date_source": ""})
            known.add(url)
    # Prefer feed dates and Norwegian originals before translated copies.
    items.sort(key=lambda item: urllib.parse.urlparse(item["url"]).path.startswith("/en/"))
    items.sort(key=lambda item: (bool(item["published"]), item["published"] or item.get("modified", "")), reverse=True)
    pages: list[dict[str, Any]] = []
    seen_articles = set()
    for item in items:
        url = item["url"]
        path = urllib.parse.urlparse(url).path.rstrip("/")
        article_key = path.rsplit("/", 1)[-1] if re.match(r"^/(?:no|en)/", path) else path
        if len(pages) >= max_articles or article_key in seen_articles:
            continue
        body = None
        old_page = retained.get(url.rstrip("/"))
        if old_page and old_page.get("snapshot_path"):
            try:
                raw = Path(old_page["snapshot_path"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() == old_page.get("content_sha256"):
                    body = raw
            except OSError:
                pass
        if body is None:
            if budget_exhausted:
                continue
            body = read_limited(url, MAX_PAGE_BYTES)
        if not body:
            continue
        html = body.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        title = item["title"] or (soup.title.get_text(" ", strip=True) if soup.title else "")
        publication = item["published"]
        publication_source = item.get("date_source", "")
        if not publication:
            publication, publication_source = _page_publication_date(soup, url)
        if publication and publication > datetime.now(timezone.utc).date().isoformat():
            publication = ""
            publication_source = ""
        text = trafilatura.extract(html, url=url, include_links=False, include_tables=False, favor_precision=True) or ""
        digest = hashlib.sha256(body).hexdigest()
        page = {
            **capture_source(body),
            "url": url, "title": title[:500], "main_text_excerpt": text[:5000], "content_sha256": digest,
            "published": publication, "modified": item.get("modified", ""),
            "date_kind": "published" if publication else item.get("date_kind", "unknown"), "date_source": publication_source,
            "discovered_via": "company_feed_or_sitemap",
            "retrieved_at": acquisition_times.get(url) or (old_page or {}).get('retrieved_at') or website_value.get('retrieved_at'),
        }
        pages.append(page)
        seen_articles.add(article_key)
        original = next((p for p in website_value.get("pages", []) if str(p.get("url", "")).rstrip("/") == url.rstrip("/")), None)
        if original is not None:
            original.update(page)
        else:
            website_value.setdefault("pages", []).append(page)
    return {
        "status": "available" if pages else "budget_exhausted" if budget_exhausted else "not_available", "feeds_and_sitemaps": sources,
        "source_attempts": source_attempts,
        "articles": [{key: item.get(key, "") for key in ("url", "title", "published", "modified", "date_kind", "date_source")} for item in items[:max_articles]],
        "captured_pages": len(pages), "dated_published_pages": sum(bool(page.get("published")) and page.get("date_kind") == "published" for page in pages),
        "requests": requests, "bytes": byte_count, "latencies_ms": latencies,
    }


def site_news_observations(profile: dict[str, Any]) -> list[dict[str, Any]]:
    website_record = profile.get("evidence", {}).get("website") or {}
    website_value = website_record.get("value") or {}
    identity = website_value.get("identity_assessment") or {}
    if website_record.get("status") != "available" or not identity.get("publishable") or not identity.get("content_attributable"):
        return []
    observations = []
    for page in website_value.get("pages") or []:
        published = str(page.get("published") or "")
        digest = str(page.get("content_sha256") or "")
        url = str(page.get("url") or "")
        title = str(page.get("title") or "").strip()
        if page.get("discovered_via") != "company_feed_or_sitemap" or page.get("date_kind") != "published" or not published or len(digest) != 64 or not title or not url.startswith(("http://", "https://")):
            continue
        eligible, reason = publication_eligibility(profile, published, url, title + ' ' + str(page.get('main_text_excerpt') or ''))
        if not eligible:
            page['publication_withheld_reason'] = reason
            continue
        observations.append({
            "id": "company-site-" + __import__("hashlib").sha256(f"{profile['organisation_number']}|{url}".encode()).hexdigest()[:24],
            "organisation_number": profile["organisation_number"],
            "platform": "company_site",
            "signal_type": "public_post",
            "source_url": url,
            "retrieved_at": page.get("retrieved_at") or website_record.get("retrieved_at"),
            "content_sha256": digest,
            "exact_entity": True,
            "identity_proof": [{"type": "verified_company_website", "score": identity.get("score"), "method": identity.get("method")}],
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_site",
            "evidence_span": title[:1200],
            "metrics": {"title": title, "activity_date": published, "activity_date_kind": page.get("date_kind"), "activity_date_source": page.get("date_source"), "interpretation": "Company-owned news; not independent sentiment."},
            "strategy": "verified_company_site_feed_or_sitemap",
        })
    return observations

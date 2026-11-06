from __future__ import annotations

import threading
import time
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from .source_store import capture_source
from typing import Any

from bs4 import BeautifulSoup
from .search_providers import provider_search, SEARCH_REQUEST_CEILING

_lock = threading.Lock()
_cache: dict[str, tuple[list[dict[str, str]], str]] = {}
_failures = 0
_open_until = 0.0
SEARCH_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "search-leads"
READ_SEARCH_CACHE = True


def _disk_leads(key, value=None):
    path = SEARCH_CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    try:
        if value is None:
            if not READ_SEARCH_CACHE: return None
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("version") == 1 and item.get("expires", 0) > time.time():
                return item["results"], item["status"]
            return None
        results, status = value
        ttl = 3600 if not results or key.startswith(("places:", "maps:")) else 21600 if key.startswith("news:") else 7 * 86400
        SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="leads-", dir=SEARCH_CACHE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "expires": time.time() + ttl, "results": results, "status": status}, handle, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def search_company_websites(query: str, *, timeout: float = 4.0, kind: str = "search", use_paid: bool = True) -> tuple[list[dict[str, str]], int, str]:
    """Return candidate results, actual HTTP attempts, and an actionable provider outcome.

    Empty search results are not provider failures and must not open the global circuit breaker.
    Search hits remain candidate leads; callers must independently verify them.
    """
    global _failures, _open_until
    clean = " ".join(query.split())
    if not clean:
        return [], 0, "empty_query"
    cache_key = kind + ":" + clean.casefold()
    cached_empty = False
    with _lock:
        if cache_key in _cache:
            cached, status = _cache[cache_key]
            if cached:
                cached_status = "cache_hit_results"
            elif status in {"results", "empty_result", "public_empty_result"}:
                cached_status = "cache_hit_empty"
            else:
                cached_status = status
            if cached or kind not in {"search", "news"} or status == "public_empty_result":
                return list(cached), 0, cached_status
            cached_empty = True
        public_circuit_open = time.monotonic() < _open_until
    disk = None
    cache_candidates = list(dict.fromkeys([cache_key, kind + ':' + clean] + (["maps:" + clean.casefold(), "maps:" + clean] if kind == "places" else [])))
    for candidate_key in cache_candidates:
        disk = _disk_leads(candidate_key)
        if disk is not None: break
    if disk is not None:
        results, status = disk
        with _lock: _cache[cache_key] = (results, status)
        if results or kind not in {"search", "news"}:
            return results, 0, "cache_hit_results" if results else "cache_hit_empty"
        cached_empty = True
    paid_results, paid_requests, paid_status = ([], 0, "empty_result") if cached_empty else provider_search(clean, kind=kind, timeout=timeout) if use_paid else ([], 0, "paid_not_requested")
    if paid_status in {"results", "empty_result"}:
        with _lock:
            if len(_cache) < 1000:
                _cache[cache_key] = (paid_results, paid_status)
        _disk_leads(cache_key, (paid_results, paid_status))
        if paid_results or kind not in {"search", "news"}:
            return paid_results, paid_requests, paid_status
    if kind == "news" and paid_requests < SEARCH_REQUEST_CEILING:
        leads, count, status = public_news_leads(clean, timeout=timeout)
        with _lock:
            _cache[cache_key] = (leads, status if leads else "public_empty_result")
        return leads, paid_requests + count, status
    if kind != "search" or paid_requests >= SEARCH_REQUEST_CEILING:
        return [], paid_requests, paid_status
    if public_circuit_open:
        return [], paid_requests, "circuit_open"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
    results: list[dict[str, str]] = []
    requests = paid_requests
    provider_succeeded = False
    parse_failed = False
    try:
        request = urllib.request.Request(
            "https://lite.duckduckgo.com/lite/",
            data=urllib.parse.urlencode({"q": clean}).encode("utf-8"),
            headers={**headers, "Accept": "text/html,application/xhtml+xml"},
        )
        requests += 1
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
        provider_succeeded = True
        soup = BeautifulSoup(raw, "lxml")
        for rank, anchor in enumerate(soup.select("a.result-link"), 1):
            url = str(anchor.get("href") or "").strip()
            title = anchor.get_text(" ", strip=True)
            row = anchor.find_parent("tr")
            sibling = row.find_next_sibling("tr") if row else None
            snippet_node = sibling.select_one("td.result-snippet") if sibling else None
            if url and title:
                results.append({"url": url, "title": title[:500], "snippet": snippet_node.get_text(" ", strip=True)[:1000] if snippet_node else "", "provider": "duckduckgo_lite", "rank": str(rank)})
        if not results:
            raise LookupError("search provider returned no links")
    except Exception as exc:
        parse_failed = provider_succeeded and not isinstance(exc, LookupError)
        # One deterministic fallback. Search output remains a lead, never a published fact.
        try:
            if requests >= SEARCH_REQUEST_CEILING:
                raise LookupError("Discovery request ceiling reached")
            url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": clean, "format": "rss"})
            request = urllib.request.Request(url, headers={**headers, "Accept": "application/rss+xml,application/xml,text/xml"})
            requests += 1
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(2_000_000)
            provider_succeeded = True
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                parse_failed = True
                raise
            for rank, node in enumerate(root.findall(".//item")[:20], 1):
                link = (node.findtext("link") or "").strip()
                title = (node.findtext("title") or "").strip()
                snippet = (node.findtext("description") or "").strip()
                if link and title:
                    results.append({"url": link, "title": title[:500], "snippet": snippet[:1000], "provider": "bing_rss", "rank": str(rank)})
        except Exception:
            results = []
    status = "results" if results else "parse_failure" if parse_failed else "empty_result" if provider_succeeded else "provider_error"
    with _lock:
        if status not in {"provider_error", "parse_failure"}:
            _failures = 0
            if len(_cache) < 200:
                _cache[cache_key] = (results, status if results else "public_empty_result")
        else:
            _failures += 1
            if _failures >= 4:
                _open_until = time.monotonic() + 60
    # Keyless search result retention remains transient. Only provider leads
    # covered by the configured API path are persisted above.
    return list(results), requests, status


def public_news_leads(query: str, *, timeout: float = 8):
    """Public RSS supplies discovery URLs only; publisher capture still decides publication."""
    endpoint = "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": query, "format": "rss"})
    try:
        request = urllib.request.Request(endpoint, headers={"User-Agent": "SignalpostResearch/1.0", "Accept": "application/rss+xml,application/xml"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000: return [], 1, "parse_failure"
        root = ET.fromstring(raw)
        capture = capture_source(raw)
        now = datetime.now(timezone.utc).isoformat()
        leads = []
        for node in root.findall(".//item")[:12]:
            link = str(node.findtext("link") or "")
            parsed = urllib.parse.urlparse(link)
            if (parsed.hostname or '').endswith('bing.com'):
                link = (urllib.parse.parse_qs(parsed.query).get('url') or [''])[0]
                parsed = urllib.parse.urlparse(link)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname: continue
            leads.append({**capture, "url": link, "title": str(node.findtext("title") or ""),
                          "snippet": BeautifulSoup(str(node.findtext("description") or ""), "lxml").get_text(" ", strip=True),
                          "provider": "bing_news_rss_lead", "retrieved_at": now, "rank": str(len(leads)+1)})
        return leads, 1, "results" if leads else "empty_result"
    except Exception:
        return [], 1, "provider_error"

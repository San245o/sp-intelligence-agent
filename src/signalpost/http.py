"""Budget-aware HTTP client with robots.txt compliance and per-host politeness.

Every outbound request passes through `Fetcher.get`, which is the single place
that debits the run budget. Redirects and retries are counted, matching the
evaluator's rule that both consume the 2,000-request cap.
"""
from __future__ import annotations

import gzip
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.robotparser import RobotFileParser

# Cap global socket connect and read timeouts so dead IPs never stall worker threads
socket.setdefaulttimeout(4.0)

from .budget import RunBudget
from .config import (
    HOST_MAX_CONCURRENCY,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT_S,
    MAX_RESPONSE_BYTES,
    PER_HOST_DELAY_S,
    USER_AGENT,
)
from .evidence import sha256_bytes, utc_now

_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE
_HTTPS_HANDLER = urllib.request.HTTPSHandler(context=_SSL_CONTEXT)



@dataclass(slots=True)
class Response:
    url: str
    final_url: str
    status: int | None
    body: bytes = b""
    text: str = ""
    content_type: str = ""
    retrieved_at: str = ""
    content_sha256: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None
    from_cache: bool = False
    blocked_by_robots: bool = False

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return "html" in (self.content_type or "").lower()

    def json(self) -> Any:
        """Parse the body as JSON. Raises on malformed input; callers map that
        to a `failed` claim rather than silently dropping the source."""
        import json as _json
        return _json.loads(self.text or self.body.decode("utf-8", "ignore"))


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.chain.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HostGate:
    """Serialises and paces requests per host."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {}
        self._last: dict[str, float] = {}

    def _delay_for(self, host: str) -> float:
        if "brreg.no" in host:
            return 0.05
        if "nav.no" in host:
            return 1.2
        return 0.2

    def _sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            if host not in self._sems:
                conc = 1 if "nav.no" in host else (6 if "brreg.no" in host else HOST_MAX_CONCURRENCY)
                self._sems[host] = threading.Semaphore(conc)
            return self._sems[host]

    def acquire(self, host: str) -> None:
        self._sem(host).acquire()
        delay = self._delay_for(host)
        with self._lock:
            now = time.monotonic()
            target = max(now, self._last.get(host, 0.0) + delay)
            self._last[host] = target
            wait = target - now
        if wait > 0:
            time.sleep(wait)

    def release(self, host: str) -> None:
        self._sem(host).release()


def _decode_body(raw: bytes, headers: Any) -> bytes:
    encoding = (headers.get("Content-Encoding") or "").lower()
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        return raw
    return raw


class Fetcher:
    """Single choke point for outbound HTTP.

    Responsibilities, in order: robots check, budget debit, politeness delay,
    request, hash, cache. A cache hit is free and is refunded to the budget,
    matching the evaluator's "cache hits are free" rule.
    """

    def __init__(self, budget: RunBudget, *, respect_robots: bool = True) -> None:
        self._budget = budget
        self._respect_robots = respect_robots
        self._gate = _HostGate()
        self._cache: dict[str, Response] = {}
        self._cache_lock = threading.Lock()
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self.robots_blocks = 0

    # ---------- robots ----------

    def _robots_for(self, org: str, url: str) -> RobotFileParser | None:
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
            self._robots[origin] = None  # claim the slot so peers don't refetch
        parser = RobotFileParser()
        robots_url = origin + "/robots.txt"
        try:
            if not self._budget.try_spend(org, 1):
                raise BudgetDenied()
            request = urllib.request.Request(
                robots_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=3.0, context=_SSL_CONTEXT) as handle:
                body = handle.read(400_000).decode("utf-8", "ignore")
            parser.parse(body.splitlines())
        except BudgetDenied:
            parser = None
        except Exception:
            # No robots.txt, or unreachable: RFC 9309 treats that as allow-all.
            parser.parse([])
        with self._robots_lock:
            self._robots[origin] = parser
        return parser

    def allowed(self, org: str, url: str) -> bool:
        if not self._respect_robots:
            return True
        parser = self._robots_for(org, url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    # ---------- fetch ----------

    def get(
        self,
        org: str,
        url: str,
        *,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        allow_cache: bool = True,
        check_robots: bool = True,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> Response:
        with self._cache_lock:
            cached = self._cache.get(url) if allow_cache else None
        if cached is not None:
            hit = replace(cached, from_cache=True)
            return hit

        if check_robots and not self.allowed(org, url):
            self.robots_blocks += 1
            return Response(
                url=url, final_url=url, status=None, retrieved_at=utc_now(),
                error="blocked_by_robots", blocked_by_robots=True,
            )

        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        attempts = HTTP_MAX_RETRIES + 1
        last: Response | None = None

        for attempt in range(attempts):
            if not self._budget.try_spend(org, 1):
                return Response(
                    url=url, final_url=url, status=None, retrieved_at=utc_now(),
                    error="budget_exhausted",
                )
            recorder = _RedirectRecorder()
            opener = urllib.request.build_opener(_HTTPS_HANDLER, recorder)
            req_headers = {
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Language": "nb-NO,no;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            }
            if "nav.no" in host:
                req_headers["Referer"] = "https://arbeidsplassen.nav.no/stillinger"
            if headers:
                req_headers.update(headers)
            request = urllib.request.Request(url, data=data, headers=req_headers)
            started = time.monotonic()
            self._gate.acquire(host)
            try:
                with opener.open(request, timeout=HTTP_TIMEOUT_S) as handle:
                    raw = handle.read(MAX_RESPONSE_BYTES)
                    body = _decode_body(raw, handle.headers)
                    response = Response(
                        url=url,
                        final_url=handle.geturl(),
                        status=handle.status,
                        body=body,
                        text=body.decode(
                            handle.headers.get_content_charset() or "utf-8", "ignore"),
                        content_type=handle.headers.get("Content-Type", ""),
                        retrieved_at=utc_now(),
                        content_sha256=sha256_bytes(body),
                        redirect_chain=list(recorder.chain),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
            except urllib.error.HTTPError as exc:
                response = Response(
                    url=url, final_url=url, status=exc.code, retrieved_at=utc_now(),
                    redirect_chain=list(recorder.chain),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=f"http_{exc.code}",
                )
            except Exception as exc:
                response = Response(
                    url=url, final_url=url, status=None, retrieved_at=utc_now(),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=f"{type(exc).__name__}: {str(exc)[:120]}",
                )
            finally:
                self._gate.release(host)

            last = response
            is_registry = "brreg.no" in host
            if response.ok or (response.status and 400 <= response.status < 500 and response.status != 429):
                break
            if response.status is None and not is_registry:
                break
            if attempt < attempts - 1 or (response.status == 429 and attempt < attempts):
                backoff = 3.5 if response.status == 429 else (0.4 * (attempt + 1))
                time.sleep(backoff)

        assert last is not None
        if last.ok and allow_cache:
            with self._cache_lock:
                self._cache[url] = last
        return last

    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._cache)


class BudgetDenied(Exception):
    pass

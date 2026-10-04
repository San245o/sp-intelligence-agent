"""Contact and identity-signal extraction from page HTML and text.

Two jobs:
  * `page_signals` pulls the regions the identity gate weighs (title, meta
    description, footer, contact block, visible text) out of one HTML document,
    so `identity.IdentitySignals` can be assembled from a real page.
  * `extract_contacts` pulls phones, emails and social profile links that back
    contact-detail claims.

Everything here is deterministic and regex/parse based. Norwegian formats are
handled explicitly: `+47` phone numbers, `postnummer poststed` address lines,
and `Org(anisasjonsnr)?. 123 456 789` / `NO 123 456 789 MVA` identifiers.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ..identity import IdentitySignals, find_org_numbers, mod11_valid

_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Norwegian numbers: strictly bounded 8 digits starting with 2-9 (Nkom national plan)
_PHONE = re.compile(
    r"(?<!\d)(?:(?:\+47|0047)[\s]?)?([2-9](?:[\s]?\d){7})(?!\d)")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_META_DESC2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']', re.I)

_SOCIAL_HOSTS = {
    "linkedin.com": "linkedin", "facebook.com": "facebook",
    "instagram.com": "instagram", "twitter.com": "x", "x.com": "x",
    "youtube.com": "youtube", "tiktok.com": "tiktok",
}


def normalize_social_url(url: str) -> dict[str, str] | None:
    """Canonicalize outbound social links and reject sharing/policy/noise endpoints."""
    try:
        url_str = str(url or "").strip().rstrip('.,)"\'')
        if not url_str or "[object" in url_str.lower():
            return None
        if not re.match(r"^https?://", url_str, re.I):
            if url_str.startswith("//"):
                url_str = "https:" + url_str
            else:
                url_str = "https://" + url_str
        parsed = urllib.parse.urlparse(url_str)
    except Exception:
        return None

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    platform = None
    for domain, label in _SOCIAL_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            platform = label
            break
    if not platform:
        return None

    # Handle Facebook plugin iframes / data-href
    if platform == "facebook" and parsed.path.startswith("/plugins/"):
        embedded = urllib.parse.parse_qs(parsed.query).get("href", [])
        if embedded:
            return normalize_social_url(embedded[0])
        return None

    raw_path = parsed.path.strip("/")
    parts = [p.strip() for p in raw_path.split("/") if p.strip()]
    lowered = [p.lower() for p in parts]

    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins", "login", "signup"},
        "instagram": {"p", "reel", "reels", "stories", "explore", "about"},
        "x": {"intent", "share", "home", "search", "i", "privacy"},
        "linkedin": {"sharearticle", "sharing", "login", "signup", "feed"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] in ("profile.php", "pages"):
        if lowered[0] == "profile.php":
            return None
        parts = parts[1:] if len(parts) > 1 else parts
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
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
    elif platform in ("facebook", "instagram", "x"):
        parts = parts[:1]

    canonical_url = f"https://{canonical_host}/{'/'.join(parts)}"
    return {"platform": platform, "url": canonical_url}


def _strip_tags(html: str) -> str:
    html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html,
                  flags=re.I | re.S)
    return re.sub(r"<[^>]+>", " ", html)


def _extract_region(html: str, tag: str) -> str:
    """Concatenated text of all `<tag>...</tag>` blocks (e.g. footer)."""
    blocks = re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, re.I | re.S)
    return " ".join(re.sub(r"\s+", " ", _strip_tags(b)).strip() for b in blocks)


def visible_text(html: str) -> str:
    """Best-effort main text. Uses trafilatura when substantive, plus tag-stripped text."""
    stripped = re.sub(r"\s+", " ", _strip_tags(html)).strip()
    try:
        import trafilatura
        extracted = trafilatura.extract(
            html, include_comments=False, include_tables=True,
            favor_recall=True)
        if extracted and len(extracted) >= len(stripped) * 0.7:
            return extracted
        elif extracted:
            return extracted + " " + stripped
    except Exception:
        pass
    return stripped


def page_signals(
    html: str, *, hostname: str = "", structured_names: list[str] | None = None
) -> IdentitySignals:
    """Assemble identity-weighted regions from one HTML page."""
    title_match = _TITLE.search(html)
    title = re.sub(r"\s+", " ", _strip_tags(title_match.group(1))).strip() if title_match else ""
    meta_match = _META_DESC.search(html) or _META_DESC2.search(html)
    meta = meta_match.group(1).strip() if meta_match else ""

    footer = _extract_region(html, "footer")
    # Contact region: any element whose class/id hints at contact/impressum.
    contact_blocks = re.findall(
        r'<(?:div|section|address)\b[^>]*(?:class|id)=["\'][^"\']*'
        r'(?:kontakt|contact|impressum|footer|address)[^"\']*["\'][^>]*>(.*?)</(?:div|section|address)>',
        html, re.I | re.S)
    contact_text = " ".join(
        re.sub(r"\s+", " ", _strip_tags(b)).strip() for b in contact_blocks[:6])

    body = visible_text(html)
    return IdentitySignals(
        hostname=hostname,
        title=title,
        meta_description=meta,
        structured_names=list(structured_names or []),
        footer_text=footer[:4000],
        contact_text=contact_text[:4000],
        body_text=body,
    )


def _clean_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    core = digits[2:] if digits.startswith("47") and len(digits) == 10 else digits
    if len(core) == 8 and core.isdigit() and core[0] in "23456789":
        if core[0] in "49":
            return f"+47 {core[:3]} {core[3:5]} {core[5:]}"
        else:
            return f"+47 {core[:2]} {core[2:4]} {core[4:6]} {core[6:]}"
    return None


def extract_contacts(html: str, text: str) -> dict[str, Any]:
    """Phones, emails, social links and any on-page org numbers.

    Org numbers are mod-11 validated here too, so a contact block that states
    the registered number becomes identity-grade corroboration downstream.
    """
    haystack = f"{text}\n{html}"

    emails: list[str] = []
    for match in _EMAIL.findall(haystack):
        low = match.lower()
        if low.endswith((".png", ".jpg", ".gif", ".svg", ".webp")):
            continue
        if low not in emails:
            emails.append(low)

    # Strip script/style tags before searching for phone numbers to prevent JS bundle hashes matching
    clean_text = _strip_tags(text or html)
    phones: list[str] = []
    for match in _PHONE.finditer(clean_text):
        cleaned = _clean_phone(match.group(0))
        if cleaned and cleaned not in phones:
            phones.append(cleaned)

    socials: dict[str, str] = {}
    social_links_list: list[dict[str, str]] = []
    seen_links: set[tuple[str, str]] = set()

    candidate_links: list[str] = []
    # 1. Harvest from anchor hrefs
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>', html, re.I):
        candidate_links.append(m.group(1))
    # 2. Harvest from data-href (e.g. Facebook page widgets)
    for m in re.finditer(r'data-href=["\']([^"\']+)["\']', html, re.I):
        candidate_links.append(m.group(1))
    # 3. Harvest from iframe src (e.g. Facebook plugins)
    for m in re.finditer(r'<iframe\b[^>]*?src=["\']([^"\']+)["\']', html, re.I):
        candidate_links.append(m.group(1))
    # 4. Harvest from schema.org / raw text urls
    for m in re.finditer(r'https?://[^\s"\'<>]+', html):
        candidate_links.append(m.group(0))

    for raw in candidate_links:
        norm = normalize_social_url(raw)
        if norm:
            key = (norm["platform"], norm["url"])
            if key not in seen_links:
                seen_links.add(key)
                social_links_list.append(norm)
                # Map platform -> url (first seen wins for single dict, full list in social_links)
                if norm["platform"] not in socials:
                    socials[norm["platform"]] = norm["url"]

    org_numbers = sorted({n for n in find_org_numbers(haystack) if mod11_valid(n)})

    result: dict[str, Any] = {}
    if emails:
        result["emails"] = emails[:10]
    if phones:
        result["phones"] = phones[:10]
    if socials:
        result["social_profiles"] = socials
        result["social_links"] = social_links_list
    if org_numbers:
        result["organisation_numbers"] = org_numbers
    return result

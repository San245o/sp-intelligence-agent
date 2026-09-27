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
from typing import Any

from ..identity import IdentitySignals, find_org_numbers, mod11_valid

_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Norwegian numbers: +47 then 8 digits, or 8 digits in 2-3-3 / 3-2-3 groupings.
_PHONE = re.compile(
    r"(?:(?:\+47|0047)[\s]?)?(?:\d[\s]?){8}(?!\d)")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_META_DESC2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']', re.I)

_SOCIAL_HOSTS = {
    "linkedin.com": "linkedin", "facebook.com": "facebook",
    "instagram.com": "instagram", "twitter.com": "twitter", "x.com": "twitter",
    "youtube.com": "youtube", "tiktok.com": "tiktok",
}


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
    digits = re.sub(r"[^\d+]", "", raw)
    core = digits[3:] if digits.startswith("+47") else (
        digits[4:] if digits.startswith("0047") else digits)
    if len(core) == 8 and core.isdigit():
        return "+47 " + core
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

    phones: list[str] = []
    for match in _PHONE.findall(text):
        cleaned = _clean_phone(match)
        if cleaned and cleaned not in phones:
            phones.append(cleaned)

    socials: dict[str, str] = {}
    for match in re.finditer(r'https?://[^\s"\'<>]+', html):
        url = match.group(0).rstrip('.,)"\'')
        low = url.lower()
        for host, label in _SOCIAL_HOSTS.items():
            if host in low and label not in socials:
                path = url.split(host)[-1].strip("/")
                clean_path = path.split("?")[0].strip("/")
                slug = clean_path.split(".")[0].lower()
                if len(clean_path) >= 2 and slug not in {
                    "share", "sharer", "intent", "login", "signup", "home",
                    "privacy", "terms", "about", "contact", "dialog", "plugins"
                }:
                    socials[label] = url

    org_numbers = sorted({n for n in find_org_numbers(haystack) if mod11_valid(n)})

    result: dict[str, Any] = {}
    if emails:
        result["emails"] = emails[:10]
    if phones:
        result["phones"] = phones[:10]
    if socials:
        result["social_profiles"] = socials
    if org_numbers:
        result["organisation_numbers"] = org_numbers
    return result

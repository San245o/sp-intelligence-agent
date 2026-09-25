"""Structured-data extraction: JSON-LD, microdata, RDFa, OpenGraph.

A company that publishes schema.org/Organization markup is handing us
identity-grade facts — legal name, address, contact points, sameAs profile
links — in a form that needs no guessing. This is the highest-precision on-site
source, so it runs before any free-text heuristics and feeds the identity gate.

Parsing is delegated to `extruct`, which is pinned. If it is unavailable or a
page has no structured data, this returns nothing and the caller falls back to
contact/text extraction; it never raises into the pipeline.
"""
from __future__ import annotations

from typing import Any

ORG_TYPES = {
    "organization", "corporation", "localbusiness", "ngo", "govermentorganization",
    "governmentorganization", "educationalorganization", "store", "professionalservice",
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("@value") or value.get("name") or "").strip()
    return str(value or "").strip()


def parse_structured(html: str, base_url: str) -> dict[str, Any]:
    """Extract organisation-level structured data from one page.

    Returns a dict with any of: names, legal_name, phones, emails, addresses,
    same_as (profile URLs), vat_ids. Absent keys mean "not present", never a
    guessed value.
    """
    try:
        import extruct
    except ModuleNotFoundError:
        return {}
    try:
        data = extruct.extract(
            html, base_url=base_url,
            syntaxes=["json-ld", "microdata", "rdfa", "opengraph"],
            uniform=True)
    except Exception:
        return {}

    names: list[str] = []
    legal_names: list[str] = []
    phones: list[str] = []
    emails: list[str] = []
    addresses: list[dict[str, Any]] = []
    same_as: list[str] = []
    vat_ids: list[str] = []

    def ingest_org(node: dict[str, Any]) -> None:
        types = {str(t).split("/")[-1].lower() for t in _as_list(node.get("@type"))}
        if not types & ORG_TYPES:
            return
        for name in _as_list(node.get("name")):
            if _text(name):
                names.append(_text(name))
        for legal in _as_list(node.get("legalName")):
            if _text(legal):
                legal_names.append(_text(legal))
        for phone in _as_list(node.get("telephone")):
            if _text(phone):
                phones.append(_text(phone))
        for email in _as_list(node.get("email")):
            if _text(email):
                emails.append(_text(email).replace("mailto:", ""))
        for vat in _as_list(node.get("vatID")) + _as_list(node.get("taxID")):
            if _text(vat):
                vat_ids.append(_text(vat))
        for addr in _as_list(node.get("address")):
            if isinstance(addr, dict):
                addresses.append({
                    "street": _text(addr.get("streetAddress")),
                    "postal_code": _text(addr.get("postalCode")),
                    "postal_town": _text(addr.get("addressLocality")),
                    "region": _text(addr.get("addressRegion")),
                    "country": _text(addr.get("addressCountry")),
                })
            elif _text(addr):
                addresses.append({"street": _text(addr)})
        for link in _as_list(node.get("sameAs")):
            if _text(link):
                same_as.append(_text(link))
        # contactPoint may nest more phones/emails
        for cp in _as_list(node.get("contactPoint")):
            if isinstance(cp, dict):
                for phone in _as_list(cp.get("telephone")):
                    if _text(phone):
                        phones.append(_text(phone))
                for email in _as_list(cp.get("email")):
                    if _text(email):
                        emails.append(_text(email).replace("mailto:", ""))

    for syntax in ("json-ld", "microdata", "rdfa"):
        for node in data.get(syntax, []) or []:
            if isinstance(node, dict):
                ingest_org(node)
                for nested in _as_list(node.get("@graph")):
                    if isinstance(nested, dict):
                        ingest_org(nested)

    # OpenGraph: site name only, weaker signal but useful for identity region.
    for node in data.get("opengraph", []) or []:
        if isinstance(node, dict):
            for key in ("og:site_name", "og:title"):
                if node.get(key):
                    names.append(str(node[key]).strip())

    def dedupe(items: list[Any]) -> list[Any]:
        out: list[Any] = []
        for item in items:
            if item and item not in out:
                out.append(item)
        return out

    result: dict[str, Any] = {}
    if names:
        result["names"] = dedupe(names)
    if legal_names:
        result["legal_names"] = dedupe(legal_names)
    if phones:
        result["phones"] = dedupe(phones)
    if emails:
        result["emails"] = dedupe(emails)
    if addresses:
        result["addresses"] = addresses
    if same_as:
        result["same_as"] = dedupe(same_as)
    if vat_ids:
        result["vat_ids"] = dedupe(vat_ids)
    return result

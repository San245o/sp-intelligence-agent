from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any
from .source_types import directory_url


LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


def _words(value: Any) -> str:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    return " ".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()))


def _phrase(needle: str, text: str) -> bool:
    return bool(needle and f" {needle} " in f" {text} ")


def _number_in_text(number: str, text: str) -> bool:
    # Never concatenate unrelated prices/phone numbers to manufacture an OrgNr.
    return bool(number and re.search(r"(?<!\d)" + r"[\s.]*".join(map(re.escape, number)) + r"(?!\d)", text))


def _owner_nodes(value: dict, host: str) -> list[dict]:
    base = value.get('url') or value.get('final_url') or ('https://' + host + '/')
    result = []
    for node in value.get('structured_organisations') or []:
        if not isinstance(node, dict): continue
        declared = node.get('url') or node.get('@id')
        if not declared: continue
        parsed = urllib.parse.urlparse(urllib.parse.urljoin(base, str(declared)))
        path = parsed.path.strip('/')
        if (parsed.hostname or '').casefold().removeprefix('www.') == host and (not path or re.fullmatch(r'(?:no|en|nb|nn|index\.html?)', path, re.I)):
            result.append(node)
    return result


def _exact_name_tokens(value: Any) -> list[str]:
    # Single-letter initials distinguish legal names (Example A vs Example B).
    return [token for token in _words(value).split() if token not in LEGAL_AND_GENERIC - {'b', 'v'}]


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website") or {}
    value = website.get("value") or {}
    core = _exact_name_tokens(profile.get("name"))
    name = " ".join(core)
    legal_name = _words(profile.get("name"))
    host = (urllib.parse.urlparse(value.get("final_url") or website.get("source_url") or "").hostname or "").casefold().removeprefix("www.")
    if directory_url(value.get("final_url") or website.get("source_url")):
        return {"status": "unverified", "score": 0.0, "publishable": False, "content_attributable": False,
            "legal_name_tokens": core, "matched_tokens": [], "reasons": ["Registry/directory listings identify a company but do not establish ownership of that website"],
            "method": "source_ownership_boundary_v1"}
    entry_path = urllib.parse.urlparse(value.get("final_url") or website.get("source_url") or "").path.strip('/')
    deep_entry = bool(entry_path and not re.fullmatch(r'(?:no|en|nb|nn|index\.html?)', entry_path, re.I))
    missing_owner_homepage = deep_entry and not value.get('ownership_homepage')
    owner = value.get("ownership_homepage") or value
    owner_nodes = _owner_nodes(owner, host)
    structured = _structured_names(owner_nodes)
    home = [str(part or "") for part in [owner.get("title"), owner.get("description"), owner.get("identity_text_excerpt"), *structured]]
    pages = [" ".join(str(page.get(key) or "") for key in ("title", "identity_text_excerpt", "main_text_excerpt")) for page in value.get("pages") or []]
    raw_home = " ".join(home)
    raw_site = " ".join([*home, str(value.get("main_text_excerpt") or ""), *pages])
    normalized_home = [_words(part) for part in home]
    normalized_pages = [_words(part) for part in pages]
    def names_company(part: str) -> bool:
        # Echoing the hostname is not an independent display-name declaration.
        without_domains = re.sub(r"\b(?:www\.)?[\w-]+(?:\.[\w-]+)*\.(?:no|com|net|org|se|dk|fi|eu|io|co|app)\b", " ", part, flags=re.I)
        if _phrase(name, " ".join(_exact_name_tokens(without_domains))):
            return True
        compact = "".join(core)
        return bool(len(core) >= 2 and len(compact) >= 8 and compact in _words(without_domains).replace(" ", ""))
    legal_owner_region = bool(legal_name and _phrase(legal_name, _words(owner.get("ownership_text_excerpt"))))
    home_brand = any(names_company(str(part or "")) for part in [owner.get("title"), owner.get("description"), owner.get("heading"), *structured]) or legal_owner_region
    named_home = home_brand
    named_page = any(names_company(part) for part in pages)
    legal_home = home_brand and any(_phrase(legal_name, part) for part in normalized_home)
    legal_page = any(_phrase(legal_name, part) for part in normalized_pages)
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    owner_identity = owner.get("ownership_text_excerpt") if "ownership_text_excerpt" in owner else raw_home
    org_home = len(org) == 9 and _number_in_text(org, str(owner_identity or ""))
    org_site = len(org) == 9 and _number_in_text(org, raw_site)
    registry = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    live = (profile.get("evidence", {}).get("registry_live", {}).get("value") or {})
    def domain(raw: Any) -> str:
        raw = str(raw or "").strip()
        return (urllib.parse.urlparse(raw if "://" in raw else "https://" + raw).hostname or "").casefold().removeprefix("www.")
    declared = {domain(raw) for raw in (profile.get("website"), registry.get("hjemmeside"), live.get("website")) if raw}
    registry_link = bool(host and any(host == item or host.endswith("." + item) for item in declared if item))
    email = str(live.get("epostadresse") or registry.get("epostadresse") or "").strip().casefold()
    mail_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    shared = {"gmail.com", "hotmail.com", "outlook.com", "online.no", "live.no", "live.com", "yahoo.com", "icloud.com", "hotmail.no", "outlook.no", "msn.com"}
    email_link = bool(host and mail_domain and mail_domain not in shared and (host == mail_domain or host.endswith("." + mail_domain)))
    street = registry.get("forretningsadresse.adresse") or registry.get("postadresse.adresse") or ""
    if isinstance(street, list):
        street = " ".join(map(str, street))
    postcode = str(registry.get("forretningsadresse.postnummer") or registry.get("postadresse.postnummer") or "")
    city = _words(registry.get("forretningsadresse.poststed") or registry.get("forretningsadresse.kommune") or profile.get("municipality"))
    normalized_site = _words(raw_site)
    address_match = len(_words(street)) >= 6 and _phrase(_words(street), normalized_site) and bool(re.fullmatch(r"\d{4}", postcode) and _number_in_text(postcode, raw_site))
    phone = re.sub(r"\D", "", str(registry.get("telefon") or registry.get("mobil") or ""))
    phone = phone.removeprefix("0047") if len(phone) == 12 else phone[2:] if len(phone) == 10 and phone.startswith("47") else phone
    phone_match = len(phone) == 8 and _number_in_text(phone, raw_site)
    local = address_match or phone_match
    roles = (profile.get("evidence", {}).get("roles", {}).get("value") or {}).get("roles") or []
    person_match = None
    for role in roles:
        person = _tokens(role.get("name"))
        if role.get("inactive") or role.get("organisation_number") or len(person) < 2 or set(person) <= set(core):
            continue
        if _phrase(" ".join(person), " ".join(_tokens(raw_site))):
            person_match = role.get("name")
            break
    requested = urllib.parse.urlparse(value.get("requested_url") or "")
    declared_urls = [profile.get("website"), registry.get("hjemmeside"), live.get("website")]
    registered_redirect = any(raw and domain(raw) == (requested.hostname or "").casefold().removeprefix("www.")
                              and (urllib.parse.urlparse(str(raw) if "://" in str(raw) else "https://" + str(raw)).path or "/").rstrip("/") == (requested.path or "/").rstrip("/")
                              for raw in declared_urls)
    name_domain = bool(name and host.endswith(".no") and host.split(".")[0].replace("-", "") == "".join(core))
    matched = sorted(set(core) & set(_tokens(raw_site)))
    foreign_orgs = {re.sub(r"\D", "", match) for match in re.findall(r"(?<!\d)[89]\d{2}[ .]*\d{3}[ .]*\d{3}(?!\d)", raw_home)}
    locations = (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations") or []
    known_orgs = {org, *(str(loc.get("organisation_number") or "") for loc in locations)}
    conflict = bool(foreign_orgs - known_orgs) and not org_home
    parked = any(marker in raw_site.casefold() for marker in ("domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting", "parkert domene", "domenet er parkert", "her flytter snart en ny gjest", "has been informing visitors", "her kommer:", "her kommer snart", "coming soon", "website under construction", "find the best information and most relevant links on all topics related to"))
    sports = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))
    score, reason, content = 0.3, "No exact legal identity or corroborated company name was found", False
    declared_owner = any(_exact_name_tokens(node.get('name') or node.get('legalName')) == core for node in owner_nodes)
    scoped_orgs = owner_nodes
    def schema_number(node):
        value = node.get('taxID') or node.get('vatID') or node.get('identifier') or ''
        if isinstance(value, dict): value = value.get('value') or ''
        return re.sub(r'\D', '', str(value))
    scoped_exact_org = any(schema_number(node) == org for node in scoped_orgs if len(org) == 9)
    scoped_conflict = any(re.fullmatch(r'[89]\d{8}', schema_number(node)) and schema_number(node) != org for node in scoped_orgs) and not scoped_exact_org
    contact_org = any(_number_in_text(org, ' '.join(str(page.get(k) or '') for k in ('identity_text_excerpt', 'main_text_excerpt')))
                      for page in value.get('pages') or [] if re.search(r'/(?:contact|kontakt|about|om-oss|personvern|privacy|legal|vilkar|terms)(?:[/.?_-]|$)', str(page.get('url') or ''), re.I))
    if parked:
        score, reason = 0.1, "Captured page is a parked or generic hosting placeholder"
    elif re.search(r"\b404\b|page not found|siden finnes ikke", str(value.get("title") or ""), re.I):
        score, reason = 0.1, "Captured page is an error page"
    elif missing_owner_homepage and not (scoped_exact_org or registry_link or email_link):
        score, reason = 0.1, 'A deep entry has no independent owner homepage or registered-domain proof'
    elif value.get('ownership_homepage_required') and not (home_brand or scoped_exact_org or org_home or registry_link or email_link):
        score, reason = 0.1, "A deep-page company mention needs captured homepage ownership or registered-domain corroboration"
    elif value.get('article_document') and not (declared_owner or registry_link or email_link):
        score, reason = 0.1, "A publisher article about a company is not evidence that the company owns the publisher's website"
    elif sports and "bedriftsidrett" not in _words(raw_site) and "b i l" not in _words(raw_site):
        reason = "Operating-company site does not establish the separate sports-club entity"
    elif conflict or scoped_conflict:
        score, reason = 0.2, "Homepage identifies another Norwegian organisation number"
    elif scoped_exact_org:
        score, reason, content = 1.0, "Domain-scoped Organization identifier matches the exact registry organisation", True
    elif contact_org and named_home:
        score, reason, content = 0.98, "Exact organisation number on a same-site contact/legal page corroborates the independently named homepage", True
    elif legal_owner_region:
        score, reason, content = 0.98, "Exact legal name is declared in captured homepage owner/contact regions", True
    elif org_home:
        score, reason, content = 1.0, "Exact organisation number appears in homepage evidence", True
    elif legal_home and len(core) >= 2:
        score, reason, content = 0.95, "Contiguous legal name appears in homepage evidence", True
    elif named_home and (registry_link or email_link or local or (name_domain and len(core) >= 2) or person_match):
        score, reason, content = 0.95, "Contiguous company name is corroborated by registry, contact evidence or its Norwegian name domain", True
    elif registered_redirect and named_home and _phrase(city, normalized_site):
        score, reason, content = 0.95, "Registered company URL redirects to a page naming the company in its registered city", True
    elif (legal_page or named_page) and local and named_home:
        score, reason, content = 0.95, "Same-site legal identity is corroborated by registered contact evidence and homepage brand", True
    elif legal_page and named_home and len(core) >= 2 and _phrase(city, normalized_site):
        score, reason, content = 0.95, "Full legal name on a same-site page agrees with the homepage name and registered city", True
    elif registry_link and email_link:
        score, reason = 0.92, "Both registered website and registered email identify this domain"
        content = named_home
    elif registry_link and any(len(token) >= 4 and token not in {"holding", "group", "invest", "eiendom", "service", "norge"}
                               and token in _tokens(owner.get("title")) and token in host.split(".")[0] for token in core):
        score, reason = 0.9, "Registered domain and homepage brand establish an associated website; wider content is not attributed to this entity"
    elif org_site or named_page or named_home:
        score, reason = 0.85, "Name or secondary-page reference needs independent legal-entity corroboration"
    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {"status": status, "score": score, "publishable": status == "exact", "content_attributable": content,
            "legal_name_tokens": core, "matched_tokens": matched, "reasons": [reason],
            "registry_domain_match": registry_link, "email_domain_match": email_link,
            "address_match": address_match, "phone_match": phone_match, "city_match": _phrase(city, normalized_site),
            "registered_person_match": person_match, "registered_redirect": registered_redirect,
            "conflicting_org": conflict, "method": "corroborated_legal_identity_v3"}


def assess_social_identity(profile: dict[str, Any], link: dict[str, str]) -> dict[str, Any]:
    core = _tokens(profile.get("name"))
    parsed = urllib.parse.urlparse(link.get("url") or "")
    handle_text = urllib.parse.unquote(parsed.path)
    handle_compact = "".join(_tokens(handle_text))
    matched = [token for token in core if token in handle_compact]
    core_compact = "".join(core)
    ratio = len(set(matched)) / len(set(core)) if core else 0.0
    if core_compact and core_compact in handle_compact:
        score = 0.98
        reason = "normalized legal-name sequence appears in the social handle"
    elif len(core) == 1 and matched:
        score = 0.95
        reason = "single distinctive legal-name token appears in the social handle"
    elif ratio >= 0.75 and len(set(matched)) >= 2:
        score = 0.9
        reason = "most legal-name tokens appear in the social handle"
    else:
        score = 0.3
        reason = "social handle lacks strong exact-entity name evidence"
    return {
        **link,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
        "method": "deterministic_social_handle_identity_v1",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any], *, candidate_source: str | None = None) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}
    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    if assessment["content_attributable"]:
        from .website import structured_social_links
        org = str(profile.get("organisation_number") or "")
        owner_record = value.get("ownership_homepage") or value
        owner_host = (urllib.parse.urlparse(str(value.get("final_url") or "")).hostname or "").casefold().removeprefix("www.")
        for organisation in _owner_nodes(owner_record, owner_host):
            identifier = organisation.get("taxID") or organisation.get("vatID") or organisation.get("identifier") or ""
            if isinstance(identifier, dict): identifier = identifier.get("value") or ""
            number = re.sub(r"\D", "", str(identifier))
            node_host = (urllib.parse.urlparse(str(organisation.get("url") or organisation.get("@id") or "")).hostname or "").casefold().removeprefix("www.")
            site_host = (urllib.parse.urlparse(str(value.get("final_url") or "")).hostname or "").casefold().removeprefix("www.")
            contradicted = bool(len(number) == 9 and number != org)
            own_schema = bool(node_host == site_host and node_host and not contradicted and (number == org or (_exact_name_tokens(organisation.get("legalName") or organisation.get("name")) == _exact_name_tokens(profile.get("name")))))
            if not own_schema:
                continue
            for link in structured_social_links({"sameAs": organisation.get("sameAs")}):
                original.append({**link, "discovery_source": "verified_site_link", "evidence_url": value.get("final_url"), "evidence_sha256": value.get("content_sha256"), "declaration": "owned_Organization.sameAs"})
        original = list({(link.get("platform"), link.get("url")): link for link in original}.values())
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(profile, link) for link in original]
    site_root = (urllib.parse.urlparse(value.get("final_url") or "").hostname or "").removeprefix("www.").split(".")[0].replace("-", "")
    for item in social_assessments:
        handle = "".join(_tokens(urllib.parse.urlparse(item.get("url") or "").path.split("/")[-1]))
        captured_link = item.get("discovery_source") == "verified_site_link" and len(str(item.get("evidence_sha256") or "")) == 64
        own_handle = bool(len(site_root) >= 4 and handle.startswith(site_root) and handle[len(site_root):] in {"", "no", "norge", "as", "official"})
        scoped_declaration = item.get("declaration") == "owned_Organization.sameAs"
        if captured_link and assessment["content_attributable"] and (own_handle or scoped_declaration):
            item.update(publishable=True, identity_score=0.95, reason="Captured link from the exact company's own website names that site's brand")
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and assessment["content_attributable"] and item["publishable"]
    ]
    website["value"] = value
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }

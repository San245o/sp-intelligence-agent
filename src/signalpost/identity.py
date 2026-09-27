"""Exact-entity identity resolution.

A material wrong-company publication blocks a run from becoming official, so
this module is deliberately conservative: a candidate is published only when
the evidence ties it to the exact legal entity, and anything short of that is
downgraded to `ambiguous` rather than guessed.

Scoring ladder, strongest first:
  1.00  organisation number appears on the page (definitive under Norwegian
        law: the number is unique to the entity and appears in site footers,
        VAT lines and contact pages)
  0.95  full legal-name token set present in identity-weighted page regions,
        plus at least one corroborating registry fact (postcode, city, phone)
  0.92  full legal-name token set in identity-weighted regions, substantive page
  0.85  most name tokens present -> review, not publishable
  <=0.60 weak or contradicted -> ambiguous

Negative evidence (directory listing, parked domain, franchise/portfolio page,
a different org number) caps the score below the publish threshold.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .config import PUBLISH_THRESHOLD, REVIEW_THRESHOLD

# Tokens that carry no entity-distinguishing signal.
LEGAL_FORMS = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "nuf", "ba",
    "bbl", "brl", "esek", "fli", "ks", "spa", "kf", "sf", "ab", "oy", "aps",
    "gmbh", "ltd", "limited", "inc", "plc", "llc", "bv", "nv",
    "borettslag", "boligbyggelag", "boligsameie", "sameiet", "sameie",
}
STOPWORDS = {"og", "and", "the", "for", "med", "av", "i", "pa", "til"}
FILLER_WORDS = {
    "norge", "norway", "nordic", "scandinavia", "holding", "invest", "eiendom",
    "drift", "service", "revisjon", "revisjonsfirmaet", "stiftelsen", "stiftelse",
    "stifting", "stiftinga",
    "gruppen", "gruppe", "group", "konsern", "partnere", "partner",
}
NOISE = LEGAL_FORMS | STOPWORDS | FILLER_WORDS
NOISE_BASE = LEGAL_FORMS | STOPWORDS

# Pages that describe many businesses rather than being one business's site.
DIRECTORY_HOSTS = {
    "proff.no", "1881.no", "gulesider.no", "purehelp.no", "bizweb.no",
    "regnskapstall.no", "enhetsregisteret.no", "brreg.no", "forvalt.no",
    "nettbedrift.no", "firmaregister.no", "opplysningen.no", "linkedin.com",
    "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com",
    "wikipedia.org", "yelp.com", "tripadvisor.com", "finn.no", "eniro.no",
    "1850.no", "byndle.no", "firmadatabasen.no", "listings.no", "vexter.no",
    "falio.no", "creditsafe.com", "kompass.com", "careerjet.no", "finansavisen.no",
    "aftenbladet.no", "foretaksinfo.no", "norwep.com", "dn.no", "e24.no",
    "norgelei.no", "bizin.eu", "acompio.com", "telefonterror.co.no", "nor47business.com",
    "styrerommet.no", "kragero-bbl.no", "bbl.no",
    "yra.no", "nol.no", "northdata.com", "dnb.com", "cylex.no", "1890.no",
    "vatverifier.com", "tracxn.com", "180.no", "infobel.com", "rosa.no",
    "nordicnet.no", "generate.no", "firmview.no", "sokfirma.no", "m.io.no",
    "bestilletransport.no", "utdanning.no",
}

PARKED_MARKERS = (
    "domain is for sale", "domenet er til salgs", "this domain is parked",
    "hugedomains", "domeneshop", "parked at", "buy this domain",
    "website coming soon", "under construction", "kommer snart",
    "her flytter snart", "default web page", "apache2 ubuntu default",
    "welcome to nginx", "index of /",
)

# A page that markets a franchise network or portfolio of companies is not the entity's page.
NETWORK_MARKERS = (
    "franchisetaker", "franchise", "vare medlemmer", "our portfolio companies",
    "portfolio companies", "portefoljeselskap", "portefoljeselskaper",
    "konsernet bestar av", "part of the group",
)


def fold(text: Any) -> str:
    """Norwegian-aware ASCII fold. Must match the domain generator's folding."""
    raw = str(text or "").translate(str.maketrans({
        "ø": "o", "Ø": "o", "å": "a", "Å": "a", "æ": "ae", "Æ": "ae",
        "é": "e", "è": "e", "ü": "u", "ä": "a", "ö": "o",
    }))
    return unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().casefold()


def name_tokens(name: Any) -> list[str]:
    """Distinguishing tokens of a legal name, order preserved, deduplicated."""
    seen: list[str] = []
    for token in re.findall(r"[a-z0-9]+", fold(name)):
        if token in NOISE or len(token) < 2:
            continue
        if token not in seen:
            seen.append(token)
    if not seen:
        for token in re.findall(r"[a-z0-9]+", fold(name)):
            if token in NOISE_BASE or len(token) < 2:
                continue
            if token not in seen:
                seen.append(token)
    return seen


def normalise_org_number(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def find_org_numbers(text: str) -> set[str]:
    """Extract 9-digit Norwegian organisation numbers from free text.

    Handles the common presentations: bare digits, space/dot grouped triples,
    and the VAT form `NO 123 456 789 MVA`.
    """
    found: set[str] = set()
    for match in re.finditer(r"\b(\d{3})[\s. ]?(\d{3})[\s. ]?(\d{3})\b", text):
        found.add("".join(match.groups()))
    for match in re.finditer(r"\bNO[\s]?(\d{9})\s?MVA\b", text, re.I):
        found.add(match.group(1))
    return found


def mod11_valid(org: str) -> bool:
    """Norwegian organisation numbers carry a mod-11 check digit.

    Used to discard 9-digit runs that are phone numbers or invoice references.
    """
    if len(org) != 9 or not org.isdigit():
        return False
    weights = (3, 2, 7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(org[:8], weights))
    remainder = total % 11
    check = 0 if remainder == 0 else 11 - remainder
    return check < 10 and check == int(org[8])


def registrable_domain(url: str) -> str:
    host = (urllib.parse.urlparse(
        url if "://" in str(url or "") else f"http://{url}").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_directory_host(url: str) -> bool:
    host = registrable_domain(url)
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS)


@dataclass(slots=True)
class IdentitySignals:
    """Text regions carrying different amounts of identity weight."""
    hostname: str = ""
    title: str = ""
    meta_description: str = ""
    structured_names: list[str] = field(default_factory=list)
    footer_text: str = ""       # footers carry org number / legal name
    contact_text: str = ""
    body_text: str = ""

    def identity_region(self) -> str:
        """Regions where a company states who it legally is."""
        return " ".join(filter(None, [
            self.title, self.meta_description, " ".join(self.structured_names),
            self.footer_text, self.contact_text, self.hostname,
        ]))

    def full_text(self) -> str:
        return " ".join(filter(None, [self.identity_region(), self.body_text]))


@dataclass(slots=True)
class IdentityVerdict:
    score: float
    status: str               # exact | review | ambiguous | rejected
    publishable: bool
    reasons: list[str]
    matched_tokens: list[str]
    expected_tokens: list[str]
    org_number_found: bool = False
    conflicting_org_numbers: list[str] = field(default_factory=list)
    proof_span: str | None = None
    method: str = "deterministic_identity_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "status": self.status,
            "publishable": self.publishable,
            "reasons": self.reasons,
            "matched_tokens": self.matched_tokens,
            "expected_tokens": self.expected_tokens,
            "organisation_number_on_page": self.org_number_found,
            "conflicting_organisation_numbers": self.conflicting_org_numbers,
            "proof_span": self.proof_span,
            "method": self.method,
        }


def _proof_span(text: str, needle: str, width: int = 120) -> str | None:
    match = re.search(re.escape(needle), text, re.I)
    if match:
        index = match.start()
    else:
        folded_text = fold(text)
        index = folded_text.find(needle.lower())
    if index < 0:
        return None
    start = max(0, index - width // 2)
    return " ".join(text[start:index + len(needle) + width // 2].split())


def assess_identity(
    profile: dict[str, Any],
    signals: IdentitySignals,
    *,
    source_url: str = "",
    origin: str = "",
) -> IdentityVerdict:
    """Decide whether `signals` prove the page belongs to `profile`'s entity."""
    org = normalise_org_number(profile.get("organisation_number"))
    expected = name_tokens(profile.get("name"))
    identity_text = signals.identity_region()
    all_text = signals.full_text()
    folded_identity = fold(identity_text)
    folded_all = fold(all_text)

    reasons: list[str] = []

    # --- hard rejections -------------------------------------------------
    if source_url and is_directory_host(source_url):
        return IdentityVerdict(
            0.0, "rejected", False,
            ["source is a business directory or social platform, not the company's own site"],
            [], expected)

    if any(marker in folded_all for marker in PARKED_MARKERS):
        return IdentityVerdict(
            0.05, "rejected", False,
            ["page is parked, for sale, or a default server placeholder"],
            [], expected)

    # --- organisation-number evidence -----------------------------------
    # Only count 9-digit runs that pass the mod-11 check, so phone numbers and
    # invoice references do not masquerade as organisation numbers.
    page_orgs = {n for n in find_org_numbers(all_text) if mod11_valid(n)}
    org_found = org in page_orgs
    conflicting = sorted(page_orgs - {org})

    def _token_match(t: str, text: str) -> bool:
        if len(t) <= 3:
            return bool(re.search(r"\b" + re.escape(t) + r"\b", text))
        return t in text

    matched = [t for t in expected if _token_match(t, folded_identity)]
    matched_anywhere = [t for t in expected if _token_match(t, folded_all)]
    ratio = len(matched) / len(expected) if expected else 0.0

    if org_found:
        span = (_proof_span(all_text, org)
                or _proof_span(all_text, f"{org[:3]} {org[3:6]} {org[6:]}"))
        reasons.append("exact organisation number present on page")
        if conflicting:
            # Both ours and others: typical of a group/portfolio page. Still
            # exact for us, but note it so a reviewer can see the context.
            reasons.append(
                f"page also lists {len(conflicting)} other organisation number(s)")
        return IdentityVerdict(
            1.0, "exact", True, reasons, matched, expected,
            org_number_found=True, conflicting_org_numbers=conflicting,
            proof_span=span)

    # A different org number and none of ours is strong negative evidence.
    if conflicting and not matched:
        return IdentityVerdict(
            0.15, "rejected", False,
            [f"page states a different organisation number ({conflicting[0]}) "
             "and does not carry this entity's name"],
            matched, expected, conflicting_org_numbers=conflicting)

    if not expected:
        return IdentityVerdict(
            0.2, "ambiguous", False,
            ["legal name has no distinguishing tokens to verify against"],
            [], expected)

    # --- name-based ladder ------------------------------------------------
    full_name_in_identity = len(matched) == len(expected)
    substantive = len(signals.body_text.strip()) >= 200

    # Check for workplace sports clubs (Bedriftsidrettslag / B.I.L.) which often share parent brand names
    is_sports_club = bool(re.search(
        r"(?:^|\s)(?:B\.?\s*I\.?\s*L\.?|BEDRIFTSIDRETTSLAG|IDRETTSLAG)(?:\s|$)",
        str(profile.get("name") or ""), re.I,
    ))
    if is_sports_club and not any(m in folded_all for m in ("bedriftsidrett", "idrettslag", "b i l", "sport")):
        return IdentityVerdict(
            0.30, "ambiguous", False,
            ["business sports-club entity points to operating company site without club evidence"],
            matched, expected)

    # Corroboration from registry facts that also appear on the page.
    corroboration: list[str] = []
    address = profile.get("business_address") or profile.get("forretningsadresse") or {}
    postcode = str(address.get("postnummer") or profile.get("postcode") or "").strip()
    city = fold(address.get("poststed") or profile.get("municipality") or address.get("kommune") or "")
    if postcode and postcode in all_text:
        corroboration.append("registered postcode")
    if city and len(city) > 3 and city in folded_all:
        corroboration.append("registered city")

    # Locality tokens from business address or municipality
    locality_matched = False
    for loc_token in name_tokens(city):
        if len(loc_token) >= 3 and loc_token in folded_all:
            locality_matched = True
            break
    if postcode and postcode in all_text:
        locality_matched = True

    domain = registrable_domain(source_url) if source_url else ""
    domain_label = fold(domain.split(".")[0]) if domain else ""
    domain_matches_name = bool(
        domain_label and "".join(expected).startswith(domain_label[:6]))

    is_norwegian_domain = bool(domain.endswith(".no"))
    has_norwegian_path = bool(re.search(r"/(nb-no|nn-no|no-no|no)(?:/|$)", source_url.lower()))
    has_norwegian_phone = bool(re.search(r"(\+47|0047)\s*\d", all_text))
    has_norwegian_words = any(w in folded_all for w in (
        "organisasjonsnummer", "org.nr", "kontakt oss", "vart selskap", "vare tjenester",
        "alle rettigheter", "personvernerklaering", "postboks", "apningstider",
        "informasjonskapsler", "informasjonskapselpolicy", "kundeservice", "vart kontor"
    ))
    has_norwegian_context = (
        is_norwegian_domain or has_norwegian_path or has_norwegian_phone
        or has_norwegian_words or locality_matched or bool(corroboration)
    )

    is_authoritative_origin = origin in ("registry", "registry_email", "universe_snapshot")

    if full_name_in_identity and corroboration:
        reasons.append("complete legal name in identity region of the page")
        reasons.append("corroborated by " + " and ".join(corroboration))
        score = 0.95
    elif full_name_in_identity and (domain_matches_name or locality_matched):
        if is_norwegian_domain or (has_norwegian_context and len(expected) >= 2) or (has_norwegian_path and len(expected) >= 1):
            reasons.append("complete legal name in identity region corroborated by domain or locality")
            score = 0.95
        else:
            reasons.append("name matched on foreign domain without Norwegian locality corroboration")
            score = 0.70
    elif ratio >= 0.75 and len(matched) >= 2 and locality_matched:
        reasons.append("most legal-name tokens present and corroborated by registered locality/postcode")
        score = 0.92
    elif is_authoritative_origin and is_norwegian_domain and (ratio >= 0.5 or (len(matched_anywhere) == len(expected) and len(expected) >= 2)) and (locality_matched or substantive):
        reasons.append("authoritative registry/seed domain corroborated by legal name tokens and Norwegian context")
        score = 0.92
    elif full_name_in_identity and substantive:
        if is_norwegian_domain and len(expected) >= 2:
            reasons.append("complete legal name in identity region of a substantive .no page")
            score = 0.92
        elif has_norwegian_context and ((len(expected) >= 2 and locality_matched) or has_norwegian_path):
            reasons.append("complete legal name in identity region corroborated by Norwegian context")
            score = 0.92
        else:
            reasons.append("single-token name or foreign domain requires explicit Norwegian locality corroboration")
            score = 0.70
    elif full_name_in_identity:
        reasons.append("complete legal name present but page has little content")
        score = 0.85
    elif ratio >= 0.75 and len(matched) >= 2:
        reasons.append("most legal-name tokens present, exact identity incomplete")
        score = 0.85
    elif len(expected) == 1 and matched and substantive and (domain_matches_name or (is_authoritative_origin and locality_matched)):
        if is_norwegian_domain and has_norwegian_context:
            reasons.append("single-token legal name matches domain/registry and page content")
            score = 0.92 if (is_authoritative_origin and locality_matched) else 0.88
        else:
            reasons.append("single-token name on foreign domain requires explicit corroboration")
            score = 0.60
    elif ratio >= 0.5 and len(matched) >= 2 and locality_matched:
        if is_authoritative_origin and is_norwegian_domain:
            reasons.append("partial legal name on registry-declared .no domain corroborated by locality")
            score = 0.92
        else:
            reasons.append("partial legal-name overlap corroborated by registered locality")
            score = 0.82
    elif ratio >= 0.5 and len(matched) >= 2:
        reasons.append("partial legal-name overlap only")
        score = 0.60
    elif matched_anywhere and not matched:
        if is_authoritative_origin and is_norwegian_domain and len(matched_anywhere) >= 2:
            reasons.append("complete legal name in page content of registry-declared .no domain")
            score = 0.92
        else:
            reasons.append("name appears only in body text, not in identity regions")
            score = 0.45
    else:
        reasons.append("no convincing exact-entity evidence on page")
        score = 0.25

    if any(marker in folded_all for marker in NETWORK_MARKERS) and score < 1.0:
        score = min(score, 0.7)
        reasons.append("page describes a franchise, group or member network")

    if conflicting:
        score = min(score, 0.7)
        reasons.append(
            f"page also states {len(conflicting)} unrelated organisation number(s)")

    status = ("exact" if score >= PUBLISH_THRESHOLD
              else "review" if score >= REVIEW_THRESHOLD
              else "ambiguous")
    span = None
    if matched:
        span = _proof_span(identity_text, matched[0]) or _proof_span(all_text, matched[0])
    elif matched_anywhere:
        span = _proof_span(all_text, matched_anywhere[0])
    return IdentityVerdict(
        score, status, score >= PUBLISH_THRESHOLD, reasons, matched or matched_anywhere, expected,
        conflicting_org_numbers=conflicting, proof_span=span)

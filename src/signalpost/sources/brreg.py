"""Brønnøysund Register Centre (BRREG) connectors.

BRREG is the authoritative register of Norwegian legal entities and the only
source that can settle exact identity, so it runs first for every company and
its output seeds the identity gate used on every later source.

Four endpoints, in descending value per request:
  enheter/{org}                     identity, address, industry, size, status
  enheter/{org}/roller              officers and board
  underenheter?overordnetEnhet=     branches / physical locations
  regnskapsregisteret/regnskap/{org}  filed annual accounts

Licence: Norwegian Licence for Open Government Data (NLOD) 2.0, attribution to
Brønnøysundregistrene. Personal data is minimised: role holders' names and role
titles are published, birth dates returned by the roles endpoint are not.
"""
from __future__ import annotations

from typing import Any

from ..evidence import (
    SOURCE_OFFICIAL_ACCOUNTS,
    SOURCE_OFFICIAL_REGISTRY,
    EvidenceStore,
    make_claim,
)
from ..http import Fetcher

BASE = "https://data.brreg.no/enhetsregisteret/api"
ACCOUNTS_BASE = "https://data.brreg.no/regnskapsregisteret/regnskap"
LICENCE = "NLOD 2.0 - Brønnøysundregistrene"
EXTRACTOR = "brreg_json_v1"

#: Roles worth publishing as leadership. The register carries many more codes
#: (auditor, accountant, contact person) that are not company leadership.
LEADERSHIP_ROLES = {
    "DAGL": "chief_executive",
    "LEDE": "board_chair",
    "NEST": "board_deputy_chair",
    "MEDL": "board_member",
    "VARA": "board_deputy_member",
    "OBS": "board_observer",
    "INNH": "sole_proprietor",
    "DTPR": "general_partner",
    "DTSO": "limited_partner",
    "KOMP": "general_partner",
    "BEST": "managing_officer",
}

#: Codes deliberately excluded from leadership claims.
NON_LEADERSHIP_ROLES = {"REVI", "REGN", "KONT", "FFØR", "HLSE", "ADOS", "BOBE"}


def _join(parts: Any) -> str:
    if isinstance(parts, list):
        return ", ".join(str(p).strip() for p in parts if str(p or "").strip())
    return str(parts or "").strip()


def _address(block: dict[str, Any] | None) -> dict[str, Any] | None:
    if not block:
        return None
    street = _join(block.get("adresse"))
    value = {
        "street": street or None,
        "postal_code": block.get("postnummer") or None,
        "postal_town": block.get("poststed") or None,
        "municipality": block.get("kommune") or None,
        "municipality_number": block.get("kommunenummer") or None,
        "country": block.get("land") or None,
        "country_code": block.get("landkode") or None,
    }
    return value if any(value.values()) else None


def _flat(block: dict[str, Any] | None) -> str:
    """One-line rendering used as the evidence span for an address claim."""
    if not block:
        return ""
    return " ".join(filter(None, [
        _join(block.get("adresse")),
        str(block.get("postnummer") or ""),
        str(block.get("poststed") or ""),
    ])).strip()


def _nace(block: dict[str, Any] | None) -> dict[str, str] | None:
    if not block or not block.get("kode"):
        return None
    return {"code": block["kode"], "description": block.get("beskrivelse") or ""}


# --------------------------------------------------------------------------
# enheter/{org}
# --------------------------------------------------------------------------

def fetch_entity(
    fetcher: Fetcher, org: str, store: EvidenceStore
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Fetch the register entry. Returns (raw entity, claims).

    A `None` entity means the organisation number is not in the register, or the
    fetch failed; the caller distinguishes those and emits the right terminal
    state rather than treating both as "nothing found".
    """
    url = f"{BASE}/enheter/{org}"
    response = fetcher.get(org, url, accept="application/json")

    if response.status == 404:
        return None, [make_claim(
            field="registry_entry", value=None, availability="not_available",
            note="organisation number is not present in Enhetsregisteret")]
    if response.status == 410:
        # BRREG returns 410 Gone for deleted/merged units and includes a body.
        return None, [make_claim(
            field="registry_entry", value=None, availability="not_available",
            note="unit has been removed from Enhetsregisteret (HTTP 410)")]
    if not response.ok:
        return None, [make_claim(
            field="registry_entry", value=None, availability="failed",
            note=f"registry fetch failed: {response.error or response.status}")]

    try:
        entity = response.json()
    except Exception as exc:
        return None, [make_claim(
            field="registry_entry", value=None, availability="failed",
            note=f"registry response was not valid JSON: {type(exc).__name__}")]
    if not isinstance(entity, dict):
        return None, [make_claim(
            field="registry_entry", value=None, availability="failed",
            note="registry response had unexpected shape")]

    def ev(span: str) -> list[str]:
        return [store.create(
            source_url=url, source_class=SOURCE_OFFICIAL_REGISTRY,
            retrieved_at=response.retrieved_at,
            content_sha256=response.content_sha256,
            claim_span=span, http_status=response.status,
            final_url=response.final_url, extractor=EXTRACTOR, licence=LICENCE)]

    claims: list[dict[str, Any]] = []

    def add(field: str, value: Any, span: str, **kwargs: Any) -> None:
        if value in (None, "", [], {}):
            return
        claims.append(make_claim(
            field=field, value=value, availability="available",
            evidence_ids=ev(span), confidence=1.0, **kwargs))

    name = entity.get("navn")
    add("legal_name", name, str(name))
    add("organisation_number", entity.get("organisasjonsnummer"),
        str(entity.get("organisasjonsnummer")))

    form = entity.get("organisasjonsform") or {}
    if form.get("kode"):
        add("organisation_form",
            {"code": form["kode"], "description": form.get("beskrivelse") or ""},
            f'{form["kode"]} {form.get("beskrivelse", "")}'.strip())

    add("registered_at", entity.get("registreringsdatoEnhetsregisteret"),
        str(entity.get("registreringsdatoEnhetsregisteret")))
    add("founded_at", entity.get("stiftelsesdato"),
        str(entity.get("stiftelsesdato")))

    address = entity.get("forretningsadresse")
    add("registered_address", _address(address), _flat(address))
    postal = entity.get("postadresse")
    if postal and postal != address:
        add("postal_address", _address(postal), _flat(postal))

    add("phone", entity.get("telefon"), str(entity.get("telefon")))
    add("website_registry", entity.get("hjemmeside"), str(entity.get("hjemmeside")))
    add("email", entity.get("epostadresse"), str(entity.get("epostadresse")))

    for index, key in enumerate(("naeringskode1", "naeringskode2", "naeringskode3"), 1):
        nace = _nace(entity.get(key))
        if nace:
            claims.append(make_claim(
                field="industry_code", value=nace, availability="available",
                evidence_ids=ev(f'{nace["code"]} {nace["description"]}'),
                confidence=1.0, subject=f"nace_{index}"))

    sector = entity.get("institusjonellSektorkode") or {}
    if sector.get("kode"):
        add("sector_code",
            {"code": sector["kode"], "description": sector.get("beskrivelse") or ""},
            f'{sector["kode"]} {sector.get("beskrivelse", "")}'.strip())

    purpose = _join(entity.get("vedtektsfestetFormaal")) or _join(entity.get("aktivitet"))
    add("stated_purpose", purpose or None, purpose)

    # --- employees. The register distinguishes "reported zero" from "never
    # reported", and the contract forbids turning a missing value into zero.
    registered = entity.get("harRegistrertAntallAnsatte")
    employees = entity.get("antallAnsatte")
    if registered and employees is not None:
        claims.append(make_claim(
            field="employees", value=int(employees), availability="available",
            evidence_ids=ev(f"antallAnsatte: {employees}"), confidence=1.0,
            reporting_period=entity.get(
                "registreringsdatoAntallAnsatteNAVAaregisteret")
            or entity.get("registreringsdatoAntallAnsatteEnhetsregisteret"),
            note="registered employee count (NAV Aa-registeret via Enhetsregisteret)"))
    else:
        claims.append(make_claim(
            field="employees", value=None, availability="not_available",
            note="entity has not reported an employee count; absence of a "
                 "report is not a count of zero"))

    # --- status. Publish only the flags the register actually sets.
    status_flags = {
        "bankrupt": entity.get("konkurs"),
        "under_liquidation": entity.get("underAvvikling"),
        "under_compulsory_liquidation": entity.get(
            "underTvangsavviklingEllerTvangsopplosning"),
    }
    active = not any(bool(v) for v in status_flags.values())
    add("operating_status",
        {"active": active, **{k: bool(v) for k, v in status_flags.items()}},
        f"konkurs={status_flags['bankrupt']} "
        f"underAvvikling={status_flags['under_liquidation']}")

    registers = {
        "foretaksregisteret": bool(entity.get("registrertIForetaksregisteret")),
        "mvaregisteret": bool(entity.get("registrertIMvaregisteret")),
        "frivillighetsregisteret": bool(entity.get("registrertIFrivillighetsregisteret")),
        "stiftelsesregisteret": bool(entity.get("registrertIStiftelsesregisteret")),
    }
    add("register_memberships", registers,
        " ".join(f"{k}={v}" for k, v in registers.items()))

    capital = entity.get("kapital") or {}
    if capital.get("belop"):
        add("share_capital",
            {"amount": capital["belop"], "currency": capital.get("valuta") or "NOK",
             "type": capital.get("type"), "shares": capital.get("antallAksjer")},
            f'{capital["belop"]} {capital.get("valuta", "NOK")}',
            reporting_period=capital.get("innfortDato"))

    for former in (entity.get("historiskeNavn") or [])[:8]:
        if former.get("navn"):
            claims.append(make_claim(
                field="former_name", value=former["navn"], availability="available",
                evidence_ids=ev(former["navn"]), confidence=1.0,
                subject=former["navn"],
                reporting_period=(former.get("tilDato") or "")[:10] or None))

    if entity.get("erIKonsern") is not None:
        add("in_corporate_group", bool(entity.get("erIKonsern")),
            f'erIKonsern={entity.get("erIKonsern")}')

    return entity, claims


# --------------------------------------------------------------------------
# enheter/{org}/roller
# --------------------------------------------------------------------------

def fetch_roles(
    fetcher: Fetcher, org: str, store: EvidenceStore
) -> list[dict[str, Any]]:
    """Officers and board members. Birth dates are deliberately discarded."""
    url = f"{BASE}/enheter/{org}/roller"
    response = fetcher.get(org, url, accept="application/json")
    if not response.ok:
        state = "not_available" if response.status == 404 else "failed"
        return [make_claim(
            field="leadership", value=None, availability=state,
            note=f"roles endpoint returned {response.error or response.status}")]

    try:
        payload = response.json()
    except Exception:
        return [make_claim(field="leadership", value=None, availability="failed",
                           note="roles response was not valid JSON")]

    claims: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for group in (payload.get("rollegrupper") or []):
        for role in (group.get("roller") or []):
            if role.get("avregistrert"):
                continue  # deregistered: historical, not current leadership
            code = ((role.get("type") or {}).get("kode") or "").upper()
            if code in NON_LEADERSHIP_ROLES or code not in LEADERSHIP_ROLES:
                continue
            person = role.get("person") or {}
            entity_holder = role.get("enhet") or {}
            if person:
                names = person.get("navn") or {}
                display = " ".join(filter(None, [
                    names.get("fornavn"), names.get("mellomnavn"),
                    names.get("etternavn")])).strip()
                holder_type = "person"
                holder_org = None
            elif entity_holder:
                display = (entity_holder.get("navn") or [""])[0] if isinstance(
                    entity_holder.get("navn"), list) else str(
                    entity_holder.get("navn") or "")
                holder_type = "organisation"
                holder_org = entity_holder.get("organisasjonsnummer")
            else:
                continue
            if not display:
                continue
            key = (display.casefold(), code)
            if key in seen:
                continue
            seen.add(key)

            title = (role.get("type") or {}).get("beskrivelse") or code
            value: dict[str, Any] = {
                "name": display,
                "role_code": code,
                "role": LEADERSHIP_ROLES[code],
                "role_title_no": title,
                "holder_type": holder_type,
            }
            if holder_org:
                value["holder_organisation_number"] = holder_org
            claims.append(make_claim(
                field="leadership", value=value, availability="available",
                evidence_ids=[store.create(
                    source_url=url, source_class=SOURCE_OFFICIAL_REGISTRY,
                    retrieved_at=response.retrieved_at,
                    content_sha256=response.content_sha256,
                    claim_span=f"{display} - {title}",
                    http_status=response.status, extractor=EXTRACTOR,
                    licence=LICENCE)],
                confidence=1.0, subject=display,
                reporting_period=group.get("sistEndret")))

    if not claims:
        claims.append(make_claim(
            field="leadership", value=None, availability="not_available",
            note="register lists no current leadership roles for this entity"))
    return claims


# --------------------------------------------------------------------------
# underenheter
# --------------------------------------------------------------------------

def fetch_subunits(
    fetcher: Fetcher, org: str, store: EvidenceStore, *, max_units: int = 20
) -> list[dict[str, Any]]:
    """Registered branches: the authoritative source for physical locations."""
    url = (f"{BASE}/underenheter?overordnetEnhet={org}"
           f"&size={max_units}&sort=navn")
    response = fetcher.get(org, url, accept="application/json")
    if not response.ok:
        return [make_claim(
            field="location", value=None, availability="failed",
            note=f"subunit search returned {response.error or response.status}")]

    try:
        payload = response.json()
    except Exception:
        return [make_claim(field="location", value=None, availability="failed",
                           note="subunit response was not valid JSON")]

    units = (payload.get("_embedded") or {}).get("underenheter") or []
    total = ((payload.get("page") or {}).get("totalElements")) or len(units)
    if not units:
        return [make_claim(
            field="location", value=None, availability="not_available",
            note="entity has no registered subunits; the registered business "
                 "address is its only known location")]

    claims: list[dict[str, Any]] = []
    for unit in units:
        address = unit.get("beliggenhetsadresse") or unit.get("postadresse")
        employees = unit.get("antallAnsatte") if unit.get(
            "harRegistrertAntallAnsatte") else None
        value = {
            "name": unit.get("navn"),
            "organisation_number": unit.get("organisasjonsnummer"),
            "address": _address(address),
            "industry_code": _nace(unit.get("naeringskode1")),
            "employees": employees,
            "registered_at": unit.get("registreringsdatoEnhetsregisteret"),
        }
        claims.append(make_claim(
            field="location", value=value, availability="available",
            evidence_ids=[store.create(
                source_url=url, source_class=SOURCE_OFFICIAL_REGISTRY,
                retrieved_at=response.retrieved_at,
                content_sha256=response.content_sha256,
                claim_span=f'{unit.get("navn")} {_flat(address)}'.strip(),
                http_status=response.status, extractor=EXTRACTOR,
                licence=LICENCE)],
            confidence=1.0, subject=str(unit.get("organisasjonsnummer") or
                                        unit.get("navn"))))

    if total > len(units):
        claims.append(make_claim(
            field="location_count", value=int(total), availability="available",
            evidence_ids=claims[0]["evidence_ids"], confidence=1.0,
            note=f"register reports {total} subunits; {len(units)} enumerated "
                 "within this run's request budget"))
    return claims


# --------------------------------------------------------------------------
# regnskapsregisteret
# --------------------------------------------------------------------------

_ACCOUNT_FIELDS = (
    # (claim field, dotted path into the account object)
    ("revenue", "resultatregnskapResultat.driftsresultat.driftsinntekter.sumDriftsinntekter"),
    ("operating_costs", "resultatregnskapResultat.driftsresultat.driftskostnad.sumDriftskostnad"),
    ("operating_result", "resultatregnskapResultat.driftsresultat.driftsresultat"),
    ("net_financial_items", "resultatregnskapResultat.finansresultat.nettoFinans"),
    ("result_before_tax", "resultatregnskapResultat.ordinaertResultatFoerSkattekostnad"),
    ("net_result", "resultatregnskapResultat.aarsresultat"),
    ("total_assets", "eiendeler.sumEiendeler"),
    ("current_assets", "eiendeler.omloepsmidler.sumOmloepsmidler"),
    ("fixed_assets", "eiendeler.anleggsmidler.sumAnleggsmidler"),
    ("equity", "egenkapitalGjeld.egenkapital.sumEgenkapital"),
    ("total_liabilities", "egenkapitalGjeld.gjeldOversikt.sumGjeld"),
    ("current_liabilities", "egenkapitalGjeld.gjeldOversikt.kortsiktigGjeld.sumKortsiktigGjeld"),
    ("long_term_liabilities", "egenkapitalGjeld.gjeldOversikt.langsiktigGjeld.sumLangsiktigGjeld"),
)


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def fetch_accounts(
    fetcher: Fetcher, org: str, store: EvidenceStore, *, max_years: int = 2
) -> list[dict[str, Any]]:
    """Filed annual accounts.

    Only entities with a filing duty appear here, so an empty result is
    `not_applicable` for a sole proprietorship and `not_available` otherwise.
    """
    url = f"{ACCOUNTS_BASE}/{org}"
    response = fetcher.get(org, url, accept="application/json")
    if not response.ok:
        if response.status in (404, 400):
            return [make_claim(
                field="annual_accounts", value=None, availability="not_available",
                note="no annual accounts filed with Regnskapsregisteret")]
        return [make_claim(
            field="annual_accounts", value=None, availability="failed",
            note=f"accounts endpoint returned {response.error or response.status}")]

    try:
        payload = response.json()
    except Exception:
        return [make_claim(field="annual_accounts", value=None,
                           availability="failed",
                           note="accounts response was not valid JSON")]

    records = payload if isinstance(payload, list) else [payload]
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return [make_claim(
            field="annual_accounts", value=None, availability="not_available",
            note="no annual accounts filed with Regnskapsregisteret")]

    # Newest first, and prefer company accounts over consolidated ones so the
    # figures describe this legal entity rather than its group.
    def sort_key(record: dict[str, Any]) -> tuple[str, int]:
        period = (record.get("regnskapsperiode") or {}).get("tilDato") or ""
        own = 0 if (record.get("regnskapstype") or "").upper() == "KONSERN" else 1
        return (period, own)

    records.sort(key=sort_key, reverse=True)
    claims: list[dict[str, Any]] = []

    for record in records[:max_years]:
        period = record.get("regnskapsperiode") or {}
        label = (period.get("tilDato") or "")[:4] or None
        currency = record.get("valuta") or "NOK"
        consolidated = (record.get("regnskapstype") or "").upper() == "KONSERN"
        base_span = (f'{period.get("fraDato")}..{period.get("tilDato")} '
                     f'{record.get("regnskapstype")} {currency}')

        def ev(span: str) -> list[str]:
            return [store.create(
                source_url=url, source_class=SOURCE_OFFICIAL_ACCOUNTS,
                retrieved_at=response.retrieved_at,
                content_sha256=response.content_sha256,
                claim_span=span, http_status=response.status,
                reporting_period=label, extractor=EXTRACTOR, licence=LICENCE)]

        for field, path in _ACCOUNT_FIELDS:
            amount = _dig(record, path)
            if amount is None:
                continue  # absent line item stays absent; never coerced to zero
            claims.append(make_claim(
                field=field,
                value={"amount": float(amount), "currency": currency,
                       "consolidated": consolidated},
                availability="available",
                evidence_ids=ev(f"{path} = {amount} {currency} | {base_span}"),
                confidence=1.0, reporting_period=label,
                subject="consolidated" if consolidated else "entity"))

        audit = record.get("revisjon") or {}
        principles = record.get("regnkapsprinsipper") or {}
        claims.append(make_claim(
            field="accounts_metadata",
            value={
                "period_start": period.get("fraDato"),
                "period_end": period.get("tilDato"),
                "currency": currency,
                "accounts_type": record.get("regnskapstype"),
                "consolidated": consolidated,
                "audited": not audit.get("ikkeRevidertAarsregnskap", False),
                "audit_waived": bool(audit.get("fravalgRevisjon")),
                "small_entity": bool(principles.get("smaaForetak")),
                "accounting_rules": principles.get("regnskapsregler"),
                "journal_number": record.get("journalnr"),
            },
            availability="available", evidence_ids=ev(base_span),
            confidence=1.0, reporting_period=label))

    return claims


# --------------------------------------------------------------------------
# konsernstruktur/{org}
# --------------------------------------------------------------------------

def fetch_group_structure(
    fetcher: Fetcher, org: str, store: EvidenceStore
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Fetch official parent entity from Brønnøysund corporate group register."""
    url = f"{BASE}/konsernstruktur/{org}"
    response = fetcher.get(org, url, accept="application/json")
    if not response.ok:
        return None, []

    try:
        payload = response.json()
    except Exception:
        return None, []

    parent_name = payload.get("parentNavn")
    parent_org = payload.get("parentOrganisasjonsnummer")
    if not parent_name and payload.get("children"):
        for child in payload.get("children", []):
            if child.get("organisasjonsnummer") == org:
                parent_name = child.get("parentNavn")
                parent_org = child.get("parentOrganisasjonsnummer")
                break

    group_info = {
        "parent_name": parent_name,
        "parent_org": parent_org,
        "raw": payload,
    }

    eid = store.create(
        source_url=url,
        source_class=SOURCE_OFFICIAL_REGISTRY,
        retrieved_at=response.retrieved_at,
        http_status=response.status,
        content_sha256=response.content_sha256,
        claim_span=f"Corporate group parent: {parent_name} ({parent_org})",
        extractor=EXTRACTOR,
        licence=LICENCE,
    )

    claims = []
    if parent_name:
        claims.append(make_claim(
            field="corporate_group_structure",
            value={
                "in_corporate_group": True,
                "parent_name": parent_name,
                "parent_org": parent_org,
            },
            availability="available",
            evidence_ids=[eid],
            confidence=1.0,
            note="official Brønnøysund corporate group parent link",
        ))

    return group_info, claims


from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "registry.name": ("name",),
    "registry.legal_form": ("legal_form",),
    "registry.employees": ("employees",),
    "registry.municipality": ("municipality",),
    "registry.website": ("website",),
    "registry.latest_submitted_accounts": ("latest_submitted_accounts",),
    "financials.records": ("evidence", "financials", "value", "records"),
    "financial_history.years": ("evidence", "financial_history", "value", "years"),
    "roles.roles": ("evidence", "roles", "value", "roles"),
    "locations.locations": ("evidence", "locations", "value", "locations"),
    "website.title": ("evidence", "website", "value", "title"),
    "website.description": ("evidence", "website", "value", "description"),
    "website.social_links": ("evidence", "website", "value", "social_links"),
}


def _read(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _evidence_for(profile: dict[str, Any], field: str) -> dict[str, Any]:
    module = field.split(".", 1)[0]
    records = profile.get("evidence", {})
    if module == "registry":
        return records.get("registry_live") or records.get("registry", {})
    return records.get(module, {})


def diff_profile(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    for field, path in TRACKED_FIELDS.items():
        old_value = _read(previous, path)
        new_value = _read(current, path)
        if old_value == new_value:
            continue
        record = _evidence_for(current, field)
        previous_record = _evidence_for(previous, field)
        changes.append({
            "organisation_number": new_org,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "source_url": record.get("source_url"),
            "retrieved_at": record.get("retrieved_at"),
            "effective_at": record.get("effective_at") or record.get("as_of"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "old_content_sha256": previous_record.get("content_sha256"),
            "new_content_sha256": record.get("content_sha256"),
            "status": record.get("status"),
        })
    old_external, new_external = _external_records(previous), _external_records(current)
    for key, item in new_external.items():
        prior = old_external.get(key)
        old_value = _external_value(prior) if prior else None
        new_value = _external_value(item)
        if old_value == new_value:
            continue
        changes.append({"organisation_number": new_org, "field": "external_footprint." + str(item.get("signal_type")),
            "change_kind": "metric_changed" if prior else "new_evidence", "observation_key": key,
            "old_value": old_value, "new_value": new_value, "source_url": item.get("source_url"),
            "retrieved_at": item.get("retrieved_at"), "effective_at": item.get("published_at") or item.get("retrieved_at"),
            "source_class": item.get("source_class"), "old_content_sha256": prior.get("content_sha256") if prior else None,
            "new_content_sha256": item.get("content_sha256"), "status": "available"})
    # A missing observation may reflect blocked discovery. Do not invent closure,
    # deletion, a headcount decline, or a zero rating from its absence.
    return changes


def _external_records(profile):
    observations = (profile.get("evidence", {}).get("external_footprint", {}).get("value") or {}).get("observations") or []
    records = {}
    for item in observations:
        if not item.get("exact_entity") or item.get("rights_status") != "approved": continue
        parsed = urlsplit(str(item.get("source_url") or ""))
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith("utm_") and k != "fbclid"])
        canonical = urlunsplit((parsed.scheme, parsed.netloc.casefold(), parsed.path.rstrip("/"), query, ""))
        key = str(item.get("platform")) + "|" + str(item.get("signal_type")) + "|" + canonical
        if item.get("signal_type") == "review": key += "|" + str(item.get("id"))
        records[key] = item
    return records


def _external_value(item):
    if item is None: return None
    metrics = {k:v for k,v in (item.get("metrics") or {}).items() if k not in {"as_of", "active_as_of", "api_url", "provider_api_url"}}
    return {"metrics": metrics, "published_at": item.get("published_at"), "sentiment_label": item.get("sentiment_label")}


def diff_datasets(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_org = {row["organisation_number"]: row for row in previous}
    new_by_org = {row["organisation_number"]: row for row in current}
    if set(old_by_org) != set(new_by_org):
        raise ValueError("Refresh datasets must have identical organisation-number membership")
    return [
        change
        for org in sorted(old_by_org)
        for change in diff_profile(old_by_org[org], new_by_org[org])
    ]

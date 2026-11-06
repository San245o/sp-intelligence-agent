"""Typed, bounded external discovery with captured sources and exact attribution.

Search snippets never become observations. Public pages obey robots policy.
Provider place statistics preserve provider attribution and captured API bytes.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

from bs4 import BeautifulSoup
from pypdf import PdfReader
import trafilatura

from .identity import _tokens, _words, _number_in_text
from .search import search_company_websites
from .search_providers import SEARCH_REQUEST_CEILING
from .source_store import capture_source
from .site_discovery import _page_publication_date
from .website import SAFE_OPENER, USER_AGENT, _robots_check, _registered_domain, assert_public_url
from .website import _social_links, _visible_text
from .identity import apply_website_identity_gate
from .discovery import BLOCKED_DISCOVERY_HOSTS
from .source_types import directory_url
from .external_footprint import publishable_observation, aggregate_footprint
from .site_jobs import _jobs
from .news_quality import publication_eligibility


def walk_json(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_json(value)


def schema_nodes(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk_json(json.loads(script.string or script.get_text() or "{}"))
        except (ValueError, TypeError):
            continue


def kinds(node):
    value = node.get("@type") or []
    return set(value if isinstance(value, list) else [value])


def own_domain(profile):
    value = (profile.get("evidence", {}).get("website", {}).get("value") or {})
    proof = value.get("identity_assessment") or {}
    return _registered_domain(value.get("final_url") or "") if proof.get("content_attributable") else ""


def legal_proof(profile, text, *, url=""):
    org = str(profile["organisation_number"])
    if _number_in_text(org, text):
        return [{"type": "captured_exact_organisation_number", "value": org}]
    domain = own_domain(profile)
    if domain and url and _registered_domain(url) == domain:
        return [{"type": "verified_own_domain", "domain": domain}]
    name = _words(profile.get("name"))
    full_name = bool(len(name) >= 6 and f" {name} " in f" {_words(text)} ")
    municipality = _words(profile.get("municipality"))
    corroborated = bool((domain and domain.casefold() in text.casefold()) or
                        (len(municipality) >= 4 and f" {municipality} " in f" {_words(text)} "))
    if full_name and corroborated:
        return [{"type": "captured_legal_name_and_domain_or_city", "legal_name": profile.get("name")}]
    core = _tokens(profile.get('name'))
    brand_match = bool(core and len(''.join(core)) >= 5 and set(core).issubset(set(_tokens(text))))
    registry = (profile.get('evidence', {}).get('registry', {}).get('value') or {})
    street = registry.get('forretningsadresse.adresse') or ''
    street = ' '.join(street) if isinstance(street, list) else str(street)
    address_match = bool(len(_words(street)) >= 6 and f' {_words(street)} ' in f' {_words(text)} ' and len(municipality) >= 4 and f' {municipality} ' in f' {_words(text)} ')
    if brand_match and address_match:
        return [{"type": "captured_brand_and_registered_street_city", "legal_name": profile.get('name'), "registered_street": street}]
    if brand_match:
        roles = (profile.get('evidence', {}).get('roles', {}).get('value') or {}).get('roles') or []
        for role in roles:
            if role.get('organisation_number') or role.get('inactive') or not isinstance(role.get('name'), str): continue
            person = _words(role.get('name'))
            if len(person) >= 10 and len(person.split()) >= 2 and f' {person} ' in f' {_words(text)} ':
                return [{"type": "captured_brand_and_current_registered_person", "registered_person": role.get('name'), "roles_source_sha256": profile.get('evidence', {}).get('roles', {}).get('content_sha256')}]
    return []


def acquire(url, budget, metrics, *, timeout=8, max_bytes=2_000_000):
    if not budget.reserve(2):
        metrics["attempts"].append({"url": url, "outcome": "budget_exhausted"})
        return None
    attempts = byte_count = 0
    started = time.monotonic()
    outcome = "source_error"
    document = None
    try:
        assert_public_url(url)
        allowed, robots_requests = _robots_check(url, timeout)
        attempts += robots_requests
        if not allowed:
            outcome = "robots_blocked"
        else:
            attempts += 1
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,application/pdf;q=0.8"})
            with SAFE_OPENER.open(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
                final_url = response.geturl()
                content_type = response.headers.get("content-type", "")
            byte_count = len(raw)
            if byte_count > max_bytes:
                outcome = "oversized"
            else:
                captured = capture_source(raw)
                soup = BeautifulSoup(raw, "lxml") if "pdf" not in content_type else None
                if soup is not None:
                    text = trafilatura.extract(raw.decode("utf-8", errors="replace"), url=final_url, include_tables=True) or soup.get_text(" ", strip=True)
                else:
                    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages[:35])
                document = {**captured, "url": final_url, "raw": raw, "soup": soup, "text": text[:100000],
                    "content_sha256": captured["snapshot_sha256"], "retrieved_at": datetime.now(timezone.utc).isoformat(), "content_type": content_type}
                outcome = "fetched"
    except Exception:
        outcome = "source_error"
    finally:
        budget.settle(2, attempts)
        metrics["requests"] += attempts
        metrics["bytes"] += byte_count
        metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
        metrics["attempts"].append({"url": url, "outcome": outcome, "requests": attempts})
    return document


def observation(profile, document, proof, signal, *, platform="news", source_class="public_news", span="", values=None, published=None):
    digest = document["content_sha256"]
    url = document["url"]
    return {"id": "external-" + hashlib.sha256(f'{profile["organisation_number"]}|{signal}|{url}|{span[:100]}'.encode()).hexdigest()[:24],
        "organisation_number": str(profile["organisation_number"]), "platform": platform, "signal_type": signal,
        "source_url": url, "retrieved_at": document["retrieved_at"], "published_at": published,
        "content_sha256": digest, "snapshot_path": document.get("snapshot_path"), "snapshot_sha256": digest,
        "exact_entity": True, "identity_proof": proof, "acquisition_mode": "permitted_public_page", "rights_status": "approved",
        "source_class": source_class, "evidence_span": span[:1500], "metrics": values or {}, "strategy": "typed_captured_external_source"}


def place_observation(profile, result):
    place = result.get("place_lead") or {}
    title = set(_tokens(place.get("title")))
    aliases = [profile.get("name"), *(profile.get("discovery_operating_names") or [])]
    named = any(bool(_tokens(name)) and set(_tokens(name)).issubset(title) for name in aliases)
    registry = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    street = registry.get("forretningsadresse.adresse") or ""
    street = " ".join(street) if isinstance(street, list) else str(street)
    postcode = str(registry.get("forretningsadresse.postnummer") or "")
    address = str(place.get("address") or "")
    address_match = bool(len(_words(street)) >= 6 and f" {_words(street)} " in f" {_words(address)} " and len(postcode) == 4 and _number_in_text(postcode, address))
    website_match = bool(own_domain(profile) and _registered_domain(str(place.get("website") or "")) == own_domain(profile))
    if not (named and address_match) or not result.get("snapshot_path"):
        return None
    try:
        rating, count = float(place["rating"]), int(place["ratingCount"])
    except (TypeError, ValueError, KeyError):
        return None
    if not 0 <= rating <= 5 or count < 1:
        return None
    document = {**result, "url": result["url"]}
    proof = [{"type": "provider_place_legal_or_operating_name_and_exact_registered_address", "website_match": website_match,
              "provider": result.get("provider"), "registered_street": street, "registered_postcode": postcode}]
    item = observation(profile, document, proof, "place_summary", platform="google_places", source_class="customer_review",
        span=f'{place.get("title")}: {rating}/5 from {count} ratings; {address}',
        values={"rating": rating, "review_count": count, "scale": 5, "place_id": place.get("placeId"), "cid": place.get("cid"),
                "provider": "serper", "provider_api_url": result.get("provider_api_url"), "interpretation": "Provider-reported aggregate; no individual review text or inferred sentiment."})
    item.update(acquisition_mode="licensed_api", strategy="serper_places_exact_address", provider="serper")
    return item


def parse_document(profile, document, *, category):
    soup, text, url = document["soup"], document["text"], document["url"]
    nodes = list(schema_nodes(soup)) if soup is not None else []
    proof = legal_proof(profile, text, url=url)
    if not proof and category == "news" and soup is not None and own_domain(profile):
        core = set(_tokens(profile.get("name")))
        article = soup.select_one("article") or soup.select_one("main") or soup
        linked = any(_registered_domain(urljoin(url, str(a.get("href") or ""))) == own_domain(profile) for a in article.select("a[href]"))
        if core and core.issubset(set(_tokens(text))) and linked:
            proof = [{"type": "captured_company_name_and_link_to_verified_company_domain", "domain": own_domain(profile)}]
    if not proof:
        for node in nodes:
            if kinds(node) & {"Organization", "Corporation", "LocalBusiness"}:
                proof = legal_proof(profile, json.dumps(node, ensure_ascii=False))
                if proof: break
            if "JobPosting" in kinds(node) and isinstance(node.get("hiringOrganization"), dict):
                proof = legal_proof(profile, json.dumps(node["hiringOrganization"], ensure_ascii=False))
                if proof: break
    if not proof:
        return [], []
    output, links = [], []
    if category == "news" and soup is not None and _registered_domain(url) != own_domain(profile):
        published, date_source = _page_publication_date(soup, url)
        if published and date.fromisoformat(published) <= datetime.now(timezone.utc).date() and publication_eligibility(profile, published, url, text)[0]:
            title = soup.title.get_text(" ", strip=True) if soup.title else text[:200]
            output.append(observation(profile, document, proof, "public_mention", span=title + " " + text[:700], published=published,
                values={"title": title, "activity_date": published, "activity_date_kind": "published", "activity_date_source": date_source,
                        "publisher_domain": _registered_domain(url)}))
    for node in nodes:
        if "Review" in kinds(node) and isinstance(node.get("itemReviewed"), dict) and _registered_domain(url) != own_domain(profile):
            review_proof = legal_proof(profile, json.dumps(node["itemReviewed"], ensure_ascii=False))
            review_text = str(node.get("reviewBody") or "").strip()
            rating = node.get("reviewRating") or {}
            try:
                value, scale = float(rating["ratingValue"]), float(rating.get("bestRating") or 5)
            except (TypeError, ValueError, KeyError):
                continue
            if review_proof and review_text and scale > 0 and 0 <= value <= scale:
                item = observation(profile, document, review_proof, "review", platform="company_directory", source_class="customer_review",
                    span=review_text, values={"rating": value, "scale": scale}, published=str(node.get("datePublished") or "")[:10] or None)
                # Explicit rating polarity is distinct from inferred text sentiment.
                ratio = value / scale
                item.update(sentiment_label="positive" if ratio >= .8 else "negative" if ratio <= .4 else "neutral",
                    sentiment_model_version="explicit-review-rating-polarity-v1", sentiment_basis="individual_review_rating_not_text_inference")
                output.append(item)
        if "JobPosting" in kinds(node):
            employer = node.get("hiringOrganization") or {}
            identifier = employer.get("identifier") or employer.get("taxID") if isinstance(employer, dict) else None
            if isinstance(identifier, dict): identifier = identifier.get("value")
            org_match = str(identifier or "").replace(" ", "").removeprefix("NO").removesuffix("MVA") == str(profile["organisation_number"])
            name_match = isinstance(employer, dict) and bool(_tokens(profile.get("name"))) and _tokens(employer.get("name")) == _tokens(profile.get("name"))
            role_url = urljoin(url, str(node.get("url") or url)).split("#")[0].rstrip("/")
            if role_url != url.split("#")[0].rstrip("/"):
                links.append(role_url); continue
            try:
                published, expires = date.fromisoformat(str(node.get("datePosted") or "")[:10]), date.fromisoformat(str(node.get("validThrough") or "")[:10])
            except ValueError:
                continue
            if (org_match or name_match) and published <= datetime.now(timezone.utc).date() <= expires and len(str(node.get("title") or "")) >= 6:
                output.append(observation(profile, document, [{"type": "captured_own_site_jobposting", "employer_org_match": org_match, "employer_name_match": name_match}],
                    "job_posting", platform="job_board", source_class="company_careers", span=str(node["title"]),
                    values={"job_title": node["title"], "published_at": published.isoformat(), "deadline": expires.isoformat()}))
        if kinds(node) & {"Organization", "Corporation", "LocalBusiness"}:
            node_text = json.dumps({key: node.get(key) for key in ("name", "legalName", "identifier", "taxID", "address")}, ensure_ascii=False)
            if not legal_proof(profile, node_text):
                continue
            employee = node.get("numberOfEmployees")
            employee = employee.get("value") if isinstance(employee, dict) else employee
            if own_domain(profile) and _registered_domain(url) == own_domain(profile) and isinstance(employee, (int, float)) and not isinstance(employee, bool) and 0 <= employee <= 10_000_000:
                output.append(observation(profile, document, proof, "workforce_snapshot", platform="company_site", source_class="company_reported_workforce",
                    span=f'{node.get("name")}: numberOfEmployees={employee}', values={"employees": employee, "measure": "employees", "scope": "company_reported", "as_of": document["retrieved_at"][:10]}))
            rating = node.get("aggregateRating")
            if isinstance(rating, dict) and _registered_domain(url) != own_domain(profile):
                try:
                    value = float(rating["ratingValue"]); count = int(rating.get("ratingCount") or rating.get("reviewCount")); scale = float(rating.get("bestRating") or 5)
                except (ValueError, TypeError, KeyError):
                    continue
                if 0 <= value <= scale and count > 0:
                    output.append(observation(profile, document, proof, "review_summary", platform="company_directory", source_class="customer_review",
                        span=f'{node.get("name")}: {value}/{scale} from {count} ratings', values={"rating": value, "review_count": count, "scale": scale}))
        if kinds(node) & {"VideoObject", "SocialMediaPosting"} and own_domain(profile) and _registered_domain(url) == own_domain(profile):
            creator = node.get("author") or node.get("creator") or {}
            if not isinstance(creator, dict) or _tokens(creator.get("name")) != _tokens(profile.get("name")):
                continue
            stats = node.get("interactionStatistic") or []
            stats = stats if isinstance(stats, list) else [stats]
            values = {}
            for stat in stats:
                if not isinstance(stat, dict): continue
                action = stat.get("interactionType") or ""
                action = action.get("@type", "") if isinstance(action, dict) else str(action).rsplit("/", 1)[-1]
                key = {"WatchAction": "views", "LikeAction": "likes", "CommentAction": "comments", "ShareAction": "shares"}.get(action)
                count = stat.get("userInteractionCount")
                if key and isinstance(count, (int, float)) and not isinstance(count, bool) and count >= 0: values[key] = int(count)
            if values:
                output.append(observation(profile, document, proof, "profile_metrics", platform="company_site", source_class="company_reported_engagement",
                    span=str(node.get("name") or node.get("headline") or "Declared media interactions"), values={**values, "scope": "company_reported_media_metadata"}))
    if "pdf" in document["content_type"] and own_domain(profile) and _registered_domain(url) == own_domain(profile) and not re.search(r"\b(?:konsern|consolidated)\b", text[:2000], re.I):
        from .workforce import extract_candidate
        count, span, status, measure = extract_candidate(text)
        if status == "accepted":
            output.append(observation(profile, document, proof, "workforce_snapshot", platform="company_site", source_class="annual_report_workforce",
                span=span, values={"employees" if measure == "employees" else "full_time_equivalents": count, "measure": measure, "scope": "standalone_company"}))
    if soup is not None and category in {"careers", "workforce", "engagement"}:
        for anchor in soup.select("a[href]"):
            target = urljoin(url, str(anchor.get("href") or ""))
            if re.search(r"(?:careers?|karriere|jobs|jobb|stillinger|annual|arsrapport|årsrapport|\.pdf(?:$|\?))", target, re.I):
                links.append(target)
    return output, list(dict.fromkeys(links))[:16]


def collect_external(profile, budget, *, categories=("places", "careers", "workforce", "news", "reviews", "engagement"), max_requests=18):
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": [], "attempts": []}
    output, statuses, seen = [], {}, set()
    broad_results = profile.get('discovery_search_leads') or None
    broad_status = "reused_website_search_leads" if broad_results else "not_searched"
    value = profile.get("evidence", {}).get("website", {}).get("value") or {}
    captured_links = []
    for page in [value, *(value.get("pages") or [])]:
        source = Path(page.get("snapshot_path") or "")
        url = page.get("url") or page.get("final_url") or ""
        if not url or not source.is_file(): continue
        seen.add(url)
        try:
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != page.get("content_sha256"): continue
            soup = BeautifulSoup(raw, "lxml")
            document = {**page, "url": url, "raw": raw, "soup": soup, "text": soup.get_text(" ", strip=True),
                "content_type": "text/html", "retrieved_at": page.get("retrieved_at") or profile.get("evidence", {}).get("website", {}).get("retrieved_at")}
            observations, links = parse_document(profile, document, category="workforce")
            output.extend(observations); captured_links.extend(links)
        except (OSError, ValueError, TypeError):
            continue
    name, city = str(profile.get("name") or ""), str(profile.get("municipality") or "")
    registry = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    street = registry.get("forretningsadresse.adresse") or ""
    street = " ".join(street) if isinstance(street, list) else str(street)
    postcode = str(registry.get("forretningsadresse.postnummer") or "")
    operating = (profile.get("discovery_operating_names") or [name])[0]
    queries = {"news": f'"{name}" "{city}"', "places": f'{operating} {street} {postcode} {city}', "reviews": f'"{name}" (anmeldelser OR reviews OR omtaler)',
        "careers": f'"{name}" (stilling OR jobb OR careers)', "workforce": f'"{name}" (årsrapport OR annual report OR ansatte)',
        "engagement": f'"{name}" (video OR YouTube OR followers)'}
    for category in categories:
        if category == 'places':
            try: operating_employees = int(profile.get('employees') or 0)
            except (TypeError, ValueError): operating_employees = 0
            holding = any(word in name.casefold() for word in ('holding', 'invest', 'eiendom', 'sameie', 'borettslag'))
            physical = str(profile.get("industry_code") or "").startswith(("45", "46", "47", "55", "56", "85", "86", "93", "95", "96", "41", "42", "43"))
            if holding or not street.strip() or len(postcode) != 4 or (operating_employees == 0 and not own_domain(profile) and not physical):
                statuses[category] = {'status': 'skipped_no_exact_place_opportunity', 'accepted': 0,
                    'reason': 'Requires an operating-business signal and a usable registered street/postcode before spending.'}
                continue
        signals = {"workforce": {"workforce_snapshot"}, "engagement": {"profile_metrics"}, "reviews": {"review", "review_summary", "place_summary"}, "careers": {"job_posting"}}
        existing = [o for o in output if o["signal_type"] in signals.get(category, set())]
        if existing:
            statuses[category] = {"status": "available", "accepted": len(existing), "reused_captured_evidence": True}; continue
        if category == "engagement":
            statuses[category] = {"status": "no_supported_public_metric_source", "accepted": 0,
                "reason": "No attributable captured media statistics. Search snippets are not engagement metrics."}; continue
        if category in {"careers", "workforce"} and captured_links:
            if category == "workforce":
                leads = [link for link in dict.fromkeys(captured_links) if link.lower().split("?")[0].endswith(".pdf")]
            else:
                leads = [link for link in dict.fromkeys(captured_links) if re.search(r"careers?|karriere|jobs?|jobb|stillinger", link, re.I)]
            for link in leads[:2]:
                if link in seen or metrics["requests"] + 2 > max_requests: continue
                seen.add(link)
                document = acquire(link, budget, metrics, max_bytes=8_000_000 if category == "workforce" else 2_000_000)
                if not document: continue
                found_items, deeper = parse_document(profile, document, category=category)
                output.extend(found_items)
                for role in [u for u in deeper if u not in seen][:1] if category == "careers" else []:
                    if metrics["requests"] + 2 > max_requests: break
                    seen.add(role)
                    role_document = acquire(role, budget, metrics)
                    if role_document: output.extend(parse_document(profile, role_document, category=category)[0])
            found = [o for o in output if o["signal_type"] == ("workforce_snapshot" if category == "workforce" else "job_posting")]
            if found:
                statuses[category] = {"status": "available", "accepted": len(found)}; continue
        kind = "places" if category == "places" else "news" if category == "news" and own_domain(profile) else "search"
        needs_search = kind != "search" or broad_results is None or category in {"reviews", "careers", "workforce"}
        if needs_search and (metrics["requests"] + SEARCH_REQUEST_CEILING > max_requests or not budget.reserve(SEARCH_REQUEST_CEILING)):
            statuses[category] = {"status": "budget_exhausted", "accepted": 0}; continue
        results, requests, status = [], 0, "provider_error"
        if not needs_search:
            results, status = broad_results, broad_status
        else:
            try:
                targeted_news = kind == "news"
                news_name = re.sub(r'\s+(?:AS|ASA|NUF|ENK|ANS|DA|SA|LTD|INC)\.?$', '', str(operating), flags=re.I).strip()
                news_name = re.sub(r'^(?:STIFTELSEN|STIFTELSE)\s+', '', news_name, flags=re.I)
                query = queries[category] if category != "news" else f'"{news_name if targeted_news else operating}"'
                results, requests, status = search_company_websites(query, kind=kind, timeout=8, use_paid=category in {"places", "news"})
            except Exception:
                pass
            finally:
                budget.settle(SEARCH_REQUEST_CEILING, requests)
            if kind == "search": broad_results, broad_status = results, status
        metrics["requests"] += requests
        metrics["attempts"].append({"category": category, "strategy": "typed_search", "outcome": status, "requests": requests, "candidates": len(results)})
        before = len(output)
        for result in results[:3]:
            if category == "places":
                item = place_observation(profile, result)
                if item: output.append(item)
                continue
            url = str(result.get("url") or "")
            if url in seen or metrics["requests"] + 2 > max_requests: continue
            # Avoid fetching unrelated hits while allowing legal-name, brand and
            # organisation-number leads. Publication always checks captured text.
            candidate_text = str(result.get("title") or "") + " " + str(result.get("snippet") or "")
            names = [name, *(profile.get("discovery_operating_names") or [])]
            if not any(bool(_tokens(n)) and set(_tokens(n)).issubset(set(_tokens(candidate_text))) for n in names) and not _number_in_text(str(profile["organisation_number"]), candidate_text):
                continue
            seen.add(url)
            document = acquire(url, budget, metrics, max_bytes=8_000_000 if category == "workforce" else 2_000_000)
            if not document: continue
            output.extend(promote_site_lead(profile, document))
            observations, links = parse_document(profile, document, category=category)
            output.extend(observations)
            for link in links[:2]:
                if link in seen or metrics["requests"] + 2 > max_requests: continue
                seen.add(link)
                secondary = acquire(link, budget, metrics, max_bytes=8_000_000 if category == "workforce" else 2_000_000)
                if secondary: output.extend(parse_document(profile, secondary, category=category)[0])
        statuses[category] = {"status": "available" if len(output) > before else "no_verified_evidence" if status in {"results", "empty_result", "cache_hit_results", "cache_hit_empty"} else status,
                              "accepted": len(output) - before, "candidates": len(results)}
    accepted = list({o["id"]: o for o in output if publishable_observation(o)}.values())
    return accepted, statuses, metrics


def promote_site_lead(profile, document):
    """Feed independently fetched search leads back through the existing site gate."""
    if own_domain(profile) or document["soup"] is None: return []
    if directory_url(document["url"]): return []
    host = (urlsplit(document["url"]).hostname or "").removeprefix("www.")
    if any(host == blocked or host.endswith('.' + blocked) for blocked in BLOCKED_DISCOVERY_HOSTS): return []
    soup = document["soup"]
    value = {k:document.get(k) for k in ("snapshot_path", "snapshot_sha256", "content_sha256")}
    value.update(final_url=document["url"], requested_url=document["url"], registered_domain=_registered_domain(document["url"]),
        title=soup.title.get_text(" ", strip=True) if soup.title else "", identity_text_excerpt=_visible_text(document["raw"].decode('utf-8', errors='replace')),
        main_text_excerpt=document["text"][:5000], structured_organisations=[node for node in schema_nodes(soup) if kinds(node) & {"Organization", "Corporation", "LocalBusiness"}],
        social_links=[{**link, "discovery_source": "verified_site_link", "evidence_url": document["url"], "evidence_sha256": document["content_sha256"]} for link in _social_links(document["url"], soup)], pages=[])
    record = {"field": "website", "status": "available", "source_type": "provider_lead_then_captured_identity_check", "source_url": document["url"],
        "retrieved_at": document["retrieved_at"], "content_sha256": document["content_sha256"], "value": value}
    gated = apply_website_identity_gate(profile, record, candidate_source="provider_lead")
    if not gated["assessment"].get("publishable") or not gated["assessment"].get("content_attributable"): return []
    compact = ''.join(_tokens(profile.get('name')))
    scoped_owner = any(''.join(_tokens(node.get('name') or node.get('legalName'))) == compact and _registered_domain(str(node.get('url') or '')) == _registered_domain(document['url']) for node in value['structured_organisations'])
    title_names_company = bool(compact and compact in ''.join(_tokens(value['title'])))
    registered_owner = bool(gated['assessment'].get('registry_domain_match') or gated['assessment'].get('email_domain_match'))
    path = urlsplit(document['url']).path.strip('/')
    if path and not re.fullmatch(r'(?:no|en|nb|nn|index\.html?)', path, re.I) and not (scoped_owner or registered_owner):
        # A deep document can describe a customer, employer or tenant. Crawl the
        # origin through normal website discovery before treating it as owned.
        return []
    article_document = any(kinds(node) & {'Article', 'NewsArticle', 'BlogPosting'} for node in schema_nodes(soup))
    if article_document and not (scoped_owner or registered_owner): return []
    if not (scoped_owner or title_names_company or registered_owner): return []
    profile.setdefault('evidence', {})['website'] = gated['website']
    output = []
    for link in gated['website']['value'].get('social_links') or []:
        item = observation(profile, document, [{"type": "verified_company_site_link", "supporting_page": document['url']}], "profile_handle", platform=link['platform'], source_class="company_social", span=link['url'], values={"profile_url": link['url']})
        item.update(source_url=link['url'], profile_url=link['url'], id='typed-handle-'+hashlib.sha256(f'{profile["organisation_number"]}|{link["platform"]}|{link["url"]}'.encode()).hexdigest()[:24])
        output.append(item)
    return output


def enrich_external(profile, budget, *, max_requests=18):
    from .ats_boards import collect_ats
    from .youtube_public import collect_youtube
    observations, statuses, metrics = collect_external(profile, budget, max_requests=max_requests)
    ats, ats_metrics = collect_ats(profile, budget)
    observations.extend(ats)
    for key in ("requests", "bytes"):
        metrics[key] += ats_metrics[key]
    metrics["latencies_ms"].extend(ats_metrics["latencies_ms"])
    metrics["attempts"].extend(ats_metrics["attempts"])
    statuses["public_ats"] = {"status": "available" if ats else "no_verified_evidence" if ats_metrics["attempts"] else "no_evidenced_board", "accepted": len(ats)}
    media, media_metrics = collect_youtube(profile, budget)
    observations.extend(media)
    for key in ("requests", "bytes"): metrics[key] += media_metrics[key]
    metrics["latencies_ms"].extend(media_metrics["latencies_ms"])
    metrics["attempts"].extend(media_metrics["attempts"])
    statuses["youtube_views"] = {"status": "available" if media else "no_verified_public_view_counts", "accepted": len(media)}
    existing = (profile.get("evidence", {}).get("external_footprint", {}).get("value") or {}).get("observations") or []
    combined = list({o["id"]: o for o in [*existing, *observations] if publishable_observation(o)}.values())
    profile["external_connector_status"] = statuses
    profile["external_coverage"] = coverage_summary(combined)
    profile["external_aggregate"] = aggregate_footprint(combined)
    return observations, metrics


def coverage_summary(observations):
    accepted = [o for o in observations if publishable_observation(o)]
    independent = {urlsplit(o["source_url"]).hostname for o in accepted if o.get("source_class") in {"public_news", "customer_review", "employee_review"}}
    return {"accepted_observations": len(accepted), "platforms": sorted({o["platform"] for o in accepted}),
        "independent_source_hosts": sorted(h for h in independent if h),
        "news_items": sum(o["platform"] == "news" and o["signal_type"] in {"public_mention", "public_post"} for o in accepted),
        "review_signals": sum(o["signal_type"] in {"review", "review_summary", "place_summary"} for o in accepted),
        "job_postings": sum(o["signal_type"] == "job_posting" for o in accepted),
        "workforce_snapshots": sum(o["signal_type"] == "workforce_snapshot" for o in accepted),
        "engagement_signals": sum(o["signal_type"] in {"profile_metrics", "buzz_metrics"} and any(k in (o.get("metrics") or {}) for k in ("views", "likes", "followers", "comments", "subscribers")) for o in accepted),
        "rating_polarity_items": sum(o.get("sentiment_basis") == "individual_review_rating_not_text_inference" for o in accepted),
        "qualification": "Source-backed development observations; independent metric/sentiment labels are still required for competition qualification."}

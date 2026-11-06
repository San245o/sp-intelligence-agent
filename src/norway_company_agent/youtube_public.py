"""Actual public video-view counts from an exact-company-linked YouTube channel."""
from __future__ import annotations
import xml.etree.ElementTree as ET
from .external_collectors import acquire, observation, own_domain
from .identity import _tokens
from signalpost.sources.youtube import extract_channel_id_from_url_or_html, YOUTUBE_FEED_URL


def collect_youtube(profile, budget):
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": [], "attempts": []}
    value = profile.get("evidence", {}).get("website", {}).get("value") or {}
    if not own_domain(profile): return [], metrics
    links = [item for item in value.get("social_link_assessments", []) if item.get("platform") == "youtube" and item.get("publishable") and item.get("discovery_source") == "verified_site_link"]
    output = []
    for link in links[:1]:
        url = str(link.get("url") or "")
        channel = extract_channel_id_from_url_or_html(url)
        if not channel:
            page = acquire(url, budget, metrics)
            if page: channel = extract_channel_id_from_url_or_html(url, page["raw"].decode("utf-8", errors="replace"))
        if not channel: continue
        document = acquire(YOUTUBE_FEED_URL.format(channel_id=channel), budget, metrics)
        if not document: continue
        try: root = ET.fromstring(document["raw"])
        except ET.ParseError: continue
        title = root.findtext("{http://www.w3.org/2005/Atom}title") or ""
        aliases = [profile.get("name"), *(profile.get("discovery_operating_names") or [])]
        if not any(_tokens(name) and set(_tokens(name)).issubset(set(_tokens(title))) for name in aliases): continue
        views = []
        for node in root.findall("{http://www.w3.org/2005/Atom}entry"):
            for stats in node.findall(".//{http://search.yahoo.com/mrss/}statistics"):
                try: count = int(stats.attrib.get("views", ""))
                except ValueError: continue
                if count >= 0: views.append(count)
        if not views: continue
        proof = [{"type": "captured_exact_company_site_channel_link", "supporting_page": link.get("evidence_url"),
                  "supporting_sha256": link.get("evidence_sha256"), "channel_id": channel, "channel_title": title}]
        output.append(observation(profile, document, proof, "profile_metrics", platform="youtube", source_class="company_linked_video_metrics",
            span=f"{title}: {sum(views)} reported views across {len(views)} returned videos", values={"views": sum(views), "videos_with_view_counts": len(views), "scope": "returned_feed_videos_only", "channel_id": channel}))
    return output, metrics

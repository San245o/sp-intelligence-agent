"""YouTube Channel & Activity connector.

Resolves company-owned YouTube channels discovered from verified website crawls
and extracts public video cadence, recent uploads, and channel metadata via
the public keyless YouTube Atom feed.
Provides evidence for Buzz/Engagement (7 pts) and Freshness/Dated Activity (3 pts).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..evidence import (
    SOURCE_COMPANY_OWNED,
    EvidenceStore,
    make_claim,
    sha256_text,
    utc_now,
)
from ..http import Fetcher

YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
EXTRACTOR = "youtube_public_feed_v1"
LICENCE = "YouTube Terms of Service / Public Atom Feed"


def extract_channel_id_from_url_or_html(url: str, html: str | None = None) -> str | None:
    """Extract YouTube channel ID from URL or page HTML."""
    # Direct channel URL: youtube.com/channel/UC...
    m = re.search(r"youtube\.com/channel/(UC[a-zA-Z0-9_-]{20,24})", url)
    if m:
        return m.group(1)

    if html:
        # Check canonical link or meta tags in HTML
        m_meta = re.search(r'itemprop="channelId"\s+content="(UC[a-zA-Z0-9_-]{20,24})"', html)
        if m_meta:
            return m_meta.group(1)
        m_ext = re.search(r'"externalId":"(UC[a-zA-Z0-9_-]{20,24})"', html)
        if m_ext:
            return m_ext.group(1)
        m_canon = re.search(r'youtube\.com/channel/(UC[a-zA-Z0-9_-]{20,24})', html)
        if m_canon:
            return m_canon.group(1)

    return None


def fetch_youtube_activity(
    fetcher: Fetcher,
    org: str,
    legal_name: str,
    youtube_url: str,
    store: EvidenceStore,
) -> list[dict[str, Any]]:
    """Resolve YouTube channel and harvest active upload cadence."""
    clean_url = str(youtube_url or "").strip()
    if not clean_url or "youtube.com" not in clean_url and "youtu.be" not in clean_url:
        return []

    channel_id = extract_channel_id_from_url_or_html(clean_url)

    # If channel ID not in URL, fetch page to resolve it
    if not channel_id:
        page_resp = fetcher.get(
            org, clean_url, accept="text/html", check_robots=True
        )
        if page_resp.ok and page_resp.text:
            channel_id = extract_channel_id_from_url_or_html(clean_url, page_resp.text)

    if not channel_id:
        return [make_claim(
            field="buzz_or_engagement",
            value=None,
            availability="ambiguous",
            note=f"YouTube URL {clean_url!r} found but public channel ID could not be resolved",
        )]

    feed_url = YOUTUBE_FEED_URL.format(channel_id=channel_id)
    feed_resp = fetcher.get(
        org, feed_url, accept="application/atom+xml,application/xml,text/xml", check_robots=False
    )

    if not feed_resp.ok:
        state = "not_available" if feed_resp.status in (404, 400) else "failed"
        return [make_claim(
            field="buzz_or_engagement",
            value=None,
            availability=state,
            note=f"YouTube channel feed returned {feed_resp.error or feed_resp.status}",
        )]

    try:
        root = ET.fromstring(feed_resp.text)
    except Exception:
        return [make_claim(
            field="buzz_or_engagement",
            value=None,
            availability="failed",
            note="YouTube feed was not valid XML",
        )]

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    author_elem = root.find("atom:author/atom:name", ns)
    channel_name = author_elem.text if author_elem is not None else legal_name

    entries = root.findall("atom:entry", ns)
    if not entries:
        ev_id = store.create(
            source_url=feed_url,
            source_class=SOURCE_COMPANY_OWNED,
            retrieved_at=feed_resp.retrieved_at or utc_now(),
            content_sha256=feed_resp.content_sha256 or sha256_text(feed_resp.text),
            claim_span=f"YouTube channel {channel_name} (ID: {channel_id}): 0 public videos",
            http_status=feed_resp.status,
            extractor=EXTRACTOR,
            licence=LICENCE,
        )
        return [make_claim(
            field="buzz_or_engagement",
            value={
                "platform": "youtube",
                "channel_id": channel_id,
                "channel_title": channel_name,
                "recent_videos_count": 0,
            },
            availability="available",
            evidence_ids=[ev_id],
            confidence=0.95,
            note="Verified company YouTube channel on record; 0 recent public videos",
        )]

    # Newest entry
    latest = entries[0]
    title_elem = latest.find("atom:title", ns)
    pub_elem = latest.find("atom:published", ns)
    link_elem = latest.find("atom:link", ns)

    latest_title = title_elem.text if title_elem is not None else ""
    latest_pub = pub_elem.text if pub_elem is not None else ""
    latest_url = link_elem.attrib.get("href") if link_elem is not None else clean_url
    date_str = latest_pub[:10] if len(latest_pub) >= 10 else None

    span = (
        f"YouTube channel {channel_name} (ID: {channel_id}): {len(entries)} recent uploads. "
        f"Latest video: {latest_title!r} published {latest_pub}"
    )

    ev_id = store.create(
        source_url=feed_url,
        source_class=SOURCE_COMPANY_OWNED,
        retrieved_at=feed_resp.retrieved_at or utc_now(),
        content_sha256=feed_resp.content_sha256 or sha256_text(feed_resp.text),
        claim_span=span,
        http_status=feed_resp.status,
        extractor=EXTRACTOR,
        licence=LICENCE,
    )

    val = {
        "platform": "youtube",
        "channel_id": channel_id,
        "channel_title": channel_name,
        "recent_videos_count": len(entries),
        "latest_video_title": latest_title,
        "latest_video_published": latest_pub,
        "latest_video_url": latest_url,
    }

    claims = [
        make_claim(
            field="buzz_or_engagement",
            value=val,
            availability="available",
            evidence_ids=[ev_id],
            confidence=0.95,
            reporting_period=date_str,
            note=f"Active YouTube channel with {len(entries)} recent videos (latest: {date_str})",
        ),
    ]

    if date_str:
        claims.append(make_claim(
            field="dated_public_activity",
            value={
                "title": f"YouTube video upload: {latest_title}",
                "date": date_str,
                "url": latest_url,
                "source": "youtube_channel",
            },
            availability="available",
            evidence_ids=[ev_id],
            confidence=0.95,
            reporting_period=date_str,
            note="Public dated video publication on company YouTube channel",
        ))

    return claims

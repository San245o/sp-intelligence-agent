from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from signalpost.identity import (
    assess_identity,
    find_org_numbers,
    fold,
    mod11_valid,
    name_tokens,
    IdentitySignals,
)
from scripts.to_contract import convert_envelope, ALLOWED_AVAILABILITIES
from scripts.run_refresh import diff_envelopes


class SignalpostAgentTests(unittest.TestCase):
    def test_mod11_check(self):
        # 976034171 is Bjørge Bygg AS (valid)
        self.assertTrue(mod11_valid("976034171"))
        # Invalid check digit
        self.assertFalse(mod11_valid("976034172"))
        # Too short / non-digits
        self.assertFalse(mod11_valid("12345"))
        self.assertFalse(mod11_valid("abcdefghi"))

    def test_name_tokens_and_fold(self):
        tokens = name_tokens("BJØRGE BYGG OG ANLEGG AS")
        self.assertIn("bjorge", tokens)
        self.assertIn("bygg", tokens)
        self.assertIn("anlegg", tokens)
        self.assertNotIn("as", tokens)
        self.assertNotIn("og", tokens)

    def test_find_org_numbers(self):
        text = "Kontakt oss: Org.nr 976 034 171 MVA eller NO976034171MVA"
        orgs = find_org_numbers(text)
        self.assertIn("976034171", orgs)

    def test_exact_org_number_on_page_is_exact(self):
        profile = {"organisation_number": "976034171", "name": "BJØRGE BYGG AS"}
        signals = IdentitySignals(
            footer_text="Org nr: 976 034 171",
            body_text="Velkommen til Bjørge Bygg AS. Vi utfører snekkerarbeid.",
        )
        verdict = assess_identity(profile, signals, source_url="https://bjorgebygg.no")
        self.assertTrue(verdict.publishable)
        self.assertEqual(verdict.status, "exact")
        self.assertEqual(verdict.score, 1.0)

    def test_locality_corroboration_promotes_local_business(self):
        profile = {
            "organisation_number": "914778271",
            "name": "Nordfjord Bygg og Anlegg AS",
            "municipality": "STRYN",
            "business_address": {"postnummer": "6783", "poststed": "STRYN"},
        }
        # Strong overlap (3/3 tokens) with locality corroboration reaches publishable
        signals_strong = IdentitySignals(
            title="Nordfjord Bygg og Anlegg",
            body_text="Vi holder til i Stryn (postnummer 6783) og leverer byggetjenester.",
        )
        verdict_strong = assess_identity(profile, signals_strong, source_url="https://nordfjordbygg.no")
        self.assertTrue(verdict_strong.publishable)
        self.assertGreaterEqual(verdict_strong.score, 0.90)

        # Partial overlap (2/3 tokens) safely quarantined without publishing
        signals_partial = IdentitySignals(
            title="Nordfjord Bygg",
            body_text="Vi holder til i Stryn og tilbyr snekkerarbeid.",
        )
        verdict_partial = assess_identity(profile, signals_partial, source_url="https://nordfjordbygg.no")
        self.assertFalse(verdict_partial.publishable)
        self.assertIn(verdict_partial.status, ("review", "ambiguous"))

    def test_sports_club_quarantined_without_club_evidence(self):
        profile = {
            "organisation_number": "996242692",
            "name": "Primulator Bedriftsidrettslag",
            "municipality": "OSLO",
        }
        signals = IdentitySignals(
            title="Primulator",
            body_text="Premium kaffemaskiner og HoReCa utstyr for profesjonelle aktører.",
        )
        verdict = assess_identity(profile, signals, source_url="https://primulator.no")
        self.assertFalse(verdict.publishable)
        self.assertLess(verdict.score, 0.5)

    def test_parked_domain_rejected(self):
        profile = {"organisation_number": "996081001", "name": "Condalign AS"}
        signals = IdentitySignals(
            title="Condalign.com is for sale",
            body_text="This domain is parked at HugeDomains. Buy this domain today.",
        )
        verdict = assess_identity(profile, signals, source_url="https://condalign.com")
        self.assertFalse(verdict.publishable)
        self.assertEqual(verdict.status, "rejected")

    def test_contract_conversion_and_availability_states(self):
        raw_envelope = {
            "organisation_number": "976034171",
            "input_name": "BJØRGE BYGG AS",
            "claims": [
                {"field": "legal_name", "value": "BJØRGE BYGG AS", "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-1"]},
                {"field": "revenue", "value": {"amount": 25000000.0, "currency": "NOK"}, "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-2"]},
            ],
            "evidence": [
                {"id": "ev-1", "source_url": "https://data.brreg.no/api", "retrieved_at": "2026-08-24T06:00:00Z"},
                {"id": "ev-2", "source_url": "https://data.brreg.no/api", "retrieved_at": "2026-08-24T06:00:01Z"},
            ],
        }
        converted = convert_envelope(raw_envelope)
        self.assertEqual(converted["organisation_number"], "976034171")
        self.assertIn("claims", converted)
        self.assertIn("run", converted)
        for claim in converted["claims"]:
            self.assertIn(claim["availability"], ALLOWED_AVAILABILITIES)

    def test_refresh_idempotency_zero_false_changes(self):
        envelope = {
            "organisation_number": "976034171",
            "claims": [
                {"field": "legal_name", "value": "BJØRGE BYGG AS", "availability": "available", "evidence_ids": ["ev-1"]},
                {"field": "latest_annual_revenue", "value": 25000000.0, "availability": "available", "evidence_ids": ["ev-2"]},
            ],
            "evidence": [
                {"id": "ev-1", "content_sha256": "abc1", "source_url": "https://example.com"},
                {"id": "ev-2", "content_sha256": "abc2", "source_url": "https://example.com"},
            ],
        }
        diff = diff_envelopes([envelope], [envelope])
        self.assertEqual(len(diff), 0)


    def test_search_gating_blocks_shells_and_allows_active(self):
        from signalpost.discovery import should_attempt_search
        from signalpost.config import TIER_SHELL, TIER_STANDARD, TIER_RICH
        # Shell tier blocked
        self.assertFalse(should_attempt_search({"name": "ACME AS"}, TIER_SHELL))
        # Holding/invest blocked even if not shell tier
        self.assertFalse(should_attempt_search({"name": "NORDIC HOLDING AS", "employees": 5}, TIER_RICH))
        # Housing cooperative blocked
        self.assertFalse(should_attempt_search({"name": "SOLBO BORETTSLAG", "legal_form": "BRL"}, TIER_RICH))
        # Active operating business allowed
        self.assertTrue(should_attempt_search({"name": "HALDEN BETONGTRANSPORT AS", "employees": 7, "legal_form": "AS"}, TIER_RICH))
        # Active AS with filed accounts allowed
    def test_serper_multi_key_fallback(self):
        import os
        from signalpost.discovery import SerperSearchProvider
        from unittest.mock import MagicMock
        try:
            os.environ["SERPER_API_KEY"] = "dead_key, live_key"
            fetcher = MagicMock()

            resp_dead = MagicMock()
            resp_dead.ok = False
            resp_dead.status = 400

            resp_live = MagicMock()
            resp_live.ok = True
            resp_live.status = 200
            resp_live.json.return_value = {"organic": [{"link": "https://live.no"}]}

            def fake_get(tag, url, **kwargs):
                headers = kwargs.get("headers", {})
                if headers.get("X-API-KEY") == "dead_key":
                    return resp_dead
                return resp_live

            fetcher.get.side_effect = fake_get

            provider = SerperSearchProvider(fetcher=fetcher)
            urls = provider.search("test query")
            self.assertEqual(urls, ["https://live.no"])
        finally:
            os.environ.pop("SERPER_API_KEY", None)

    def test_foreign_com_single_token_quarantined(self):
        profile = {"organisation_number": "982112958", "name": "LANTECH AS", "legal_form": "AS", "municipality": "OSLO"}
        signals = IdentitySignals(
            hostname="lantech.com",
            title="Lantech - Packaging Solutions",
            body_text="Copyright 2024 Lantech Inc, Louisville, Kentucky. Stretch wrappers and pallet solutions worldwide."
        )
        verdict = assess_identity(profile, signals, source_url="https://lantech.com")
        self.assertFalse(verdict.publishable)
        self.assertLessEqual(verdict.score, 0.70)

    def test_norwegian_path_and_corporate_filler_camfil(self):
        profile = {"organisation_number": "915512992", "name": "CAMFIL NORGE AS", "legal_form": "AS", "municipality": "OSLO", "forretningsadresse": {"postnummer": "0150", "poststed": "OSLO"}}
        signals = IdentitySignals(
            hostname="camfil.com",
            title="Camfil Norge | Ren luft for alle",
            body_text="Camfil Norge leverer renluftsløsninger for bygg og industri. Postnummer 0150 Oslo. Personvern policy Informasjonskapselpolicy. Kontakt oss."
        )
        verdict = assess_identity(profile, signals, source_url="https://www.camfil.com/nb-no")
        self.assertTrue(verdict.publishable)
        self.assertGreaterEqual(verdict.score, 0.90)

    def test_short_token_word_boundary_llg(self):
        profile = {"organisation_number": "934196066", "name": "LL&G AS", "legal_form": "AS", "municipality": "LUNNER"}
        signals = IdentitySignals(
            hostname="norgelei.no",
            title="Norge LEI - Offisiell LEI Registrering",
            body_text="Offisiell registreringsagent i Norge for bedrifter som trenger LEI-kode."
        )
        verdict = assess_identity(profile, signals, source_url="https://norgelei.no")
        self.assertFalse(verdict.publishable)
        self.assertNotIn("ll", verdict.matched_tokens)

    def test_social_profile_path_filtering(self):
        from signalpost.extract.contact import extract_contacts
        html = """
        <footer>
            <a href="https://x.com/">Twitter Root</a>
            <a href="https://facebook.com/share.php">FB Share</a>
            <a href="https://linkedin.com/company/teqva-ror">LinkedIn Company</a>
        </footer>
        """
        res = extract_contacts(html, html)
        socials = res.get("social_profiles", {})
        self.assertNotIn("twitter", socials)
        self.assertNotIn("facebook", socials)
        self.assertEqual(socials.get("linkedin"), "https://linkedin.com/company/teqva-ror")

    def test_registry_trade_name_alias_published(self):
        profile = {
            "organisation_number": "911442493",
            "name": "BJØRN ENGEBRETSEN AS",
            "legal_form": "AS",
            "municipality": "OSLO",
            "forretningsadresse": {"postnummer": "0150", "poststed": "OSLO"},
        }
        signals = IdentitySignals(
            hostname="gullsmedhuset.no",
            title="Velkommen til Gullsmedhuset | Gullsmedhuset",
            body_text="Velkommen til vår butikk. Gullsmedhuset Bjørn Engebretsen leverer unike smykker. Postnummer 0150 Oslo. Kontakt oss på engebretsen@gullsmedhuset.no.",
        )
        verdict = assess_identity(
            profile, signals, source_url="https://gullsmedhuset.no", origin="registry"
        )
        self.assertTrue(verdict.publishable)
        self.assertGreaterEqual(verdict.score, 0.90)
        self.assertIn("bjorn", verdict.matched_tokens)
        self.assertIn("engebretsen", verdict.matched_tokens)

    def test_nav_name_matching_and_gating(self):
        from signalpost.sources.nav import names_match, fold, strip_legal_form
        # Exact match
        self.assertTrue(names_match("Trafikk Gjengen AS", "TRAFIKK GJENGEN AS"))
        # Match with legal form stripping
        self.assertTrue(names_match("Trafikk Gjengen AS", "Trafikk Gjengen"))
        self.assertTrue(names_match("Trafikk Gjengen", "Trafikk Gjengen AS"))
        # Norwegian diacritics folding
        self.assertTrue(names_match("Blåbær Skog ASA", "BLAABAER SKOG"))
        # Partial / different companies must NOT match
        self.assertFalse(names_match("Trafikk Gjengen AS", "Gjengen AS"))
        self.assertFalse(names_match("Trafikk Gjengen AS", "Annen Trafikk AS"))
        self.assertFalse(names_match("Trafikk Gjengen AS", "Trafikk Gjengen Entreprenør AS"))
        self.assertFalse(names_match("", "Trafikk Gjengen AS"))

    def test_nav_job_fetching_and_rejection_of_unrelated_employers(self):
        from unittest.mock import MagicMock
        from signalpost.sources.nav import fetch_nav_jobs
        from signalpost.evidence import EvidenceStore

        fetcher = MagicMock()
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status = 200
        mock_response.retrieved_at = "2026-09-28T09:00:00Z"
        mock_response.content_sha256 = "abc123"
        mock_response.json.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "uuid": "ad-1",
                            "status": "ACTIVE",
                            "title": "Tømrer søkes",
                            "businessName": "Acme Bygg AS",
                            "employer": {"name": "ACME BYGG AS"},
                            "published": "2026-09-20T10:00:00",
                            "expires": "2026-10-20T10:00:00",
                            "locationList": [{"city": "Oslo"}],
                        }
                    },
                    {
                        "_source": {
                            "uuid": "ad-2",
                            "status": "ACTIVE",
                            "title": "Sveiser",
                            "businessName": "Unrelated Entreprenør AS",
                            "employer": {"name": "Unrelated Entreprenør AS"},
                            "published": "2026-09-21T10:00:00",
                        }
                    }
                ]
            }
        }
        fetcher.get.return_value = mock_response
        store = EvidenceStore()

        claims = fetch_nav_jobs(fetcher, "912345678", "Acme Bygg AS", store)
        fields = {c["field"]: c for c in claims}

        self.assertIn("job_posting", fields)
        self.assertEqual(fields["job_posting"]["value"]["title"], "Tømrer søkes")
        self.assertEqual(fields["active_job_count"]["value"], 1)
        self.assertEqual(fields["hiring_or_activity_signal"]["value"]["active_job_ads"], 1)

    def test_filing_history_and_registry_updates(self):
        from unittest.mock import MagicMock
        from signalpost.sources.brreg import fetch_filing_history, fetch_registry_updates
        from signalpost.evidence import EvidenceStore

        store = EvidenceStore()
        fetcher = MagicMock()

        # 1. Filing history
        resp_history = MagicMock()
        resp_history.ok = True
        resp_history.status = 200
        resp_history.retrieved_at = "2026-09-28T09:00:00Z"
        resp_history.content_sha256 = "sha_hist"
        resp_history.json.return_value = ["2023", "2021", "2022", "2024"]

        fetcher.get.return_value = resp_history
        claims = fetch_filing_history(fetcher, "912345678", store)
        fields = {c["field"]: c for c in claims}

        self.assertIn("accounts_filing_years", fields)
        self.assertEqual(fields["accounts_filing_years"]["value"], ["2021", "2022", "2023", "2024"])
        self.assertEqual(fields["first_filing_year"]["value"], 2021)
        self.assertEqual(fields["filings_on_file"]["value"], 4)

        # 2. Registry updates
        resp_updates = MagicMock()
        resp_updates.ok = True
        resp_updates.status = 200
        resp_updates.retrieved_at = "2026-09-28T09:00:00Z"
        resp_updates.content_sha256 = "sha_upd"
        resp_updates.json.return_value = {
            "_embedded": {
                "oppdaterteEnheter": [
                    {"oppdateringsid": 100, "dato": "2025-01-10T12:00:00Z", "endringstype": "Ny"},
                    {"oppdateringsid": 200, "dato": "2026-06-15T08:30:00Z", "endringstype": "Endring"},
                ]
            }
        }
        fetcher.get.return_value = resp_updates
        claims_upd = fetch_registry_updates(fetcher, "912345678", store)
        fields_upd = {c["field"]: c for c in claims_upd}

        self.assertIn("registry_update", fields_upd)
        self.assertEqual(fields_upd["registry_update"]["value"]["latest_update_date"], "2026-06-15")
        self.assertEqual(fields_upd["registry_update"]["value"]["change_type"], "Endring")
        self.assertIn("dated_public_activity", fields_upd)
        self.assertEqual(fields_upd["dated_public_activity"]["value"]["date"], "2026-06-15")

    def test_to_contract_includes_new_fields(self):
        raw_env = {
            "organisation_number": "912345678",
            "input_name": "TEST BEDRIFT AS",
            "claims": [
                {"field": "legal_name", "value": "TEST BEDRIFT AS", "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-1"]},
                {"field": "accounts_filing_years", "value": ["2022", "2023", "2024"], "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-2"]},
                {"field": "active_job_count", "value": 2, "availability": "available", "confidence": 0.95, "evidence_ids": ["ev-3"]},
                {"field": "job_posting", "value": {"title": "Utvikler", "url": "https://example.com/ad"}, "availability": "available", "confidence": 0.95, "evidence_ids": ["ev-3"]},
                {"field": "registry_update", "value": {"latest_update_date": "2026-05-01", "change_type": "Endring"}, "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-4"]},
            ],
            "evidence": [
                {"id": "ev-1", "source_url": "https://data.brreg.no", "retrieved_at": "2026-09-28T09:00:00Z"},
                {"id": "ev-2", "source_url": "https://data.brreg.no/aar", "retrieved_at": "2026-09-28T09:00:00Z"},
                {"id": "ev-3", "source_url": "https://arbeidsplassen.nav.no", "retrieved_at": "2026-09-28T09:00:00Z"},
                {"id": "ev-4", "source_url": "https://data.brreg.no/oppdateringer", "retrieved_at": "2026-09-28T09:00:00Z"},
            ]
        }
        converted = convert_envelope(raw_env)
        c_map = {c["field"]: c for c in converted["claims"]}

        self.assertIn("accounts_filing_years", c_map)
        self.assertEqual(c_map["accounts_filing_years"]["value"], ["2022", "2023", "2024"])

        self.assertIn("active_job_count", c_map)
        self.assertEqual(c_map["active_job_count"]["value"], 2)

        self.assertIn("job_posting", c_map)
        self.assertEqual(c_map["job_posting"]["value"]["title"], "Utvikler")

        self.assertIn("dated_public_activity", c_map)
        self.assertEqual(c_map["dated_public_activity"]["value"]["latest_update_date"], "2026-05-01")

    def test_places_ratings_matching_and_gating(self):
        from unittest.mock import MagicMock
        from signalpost.sources.places import fetch_places_ratings, _names_match_place, _locations_match
        from signalpost.evidence import EvidenceStore

        # Name matching
        self.assertTrue(_names_match_place("Grand Hotel AS", "Grand Hotel"))
        self.assertTrue(_names_match_place("Equinor ASA", "Equinor"))
        self.assertFalse(_names_match_place("Grand Hotel AS", "Totally Different Hotel"))

        # Location matching
        self.assertTrue(_locations_match("Karl Johans gate 31, 0159 Oslo", "Oslo", None))
        self.assertFalse(_locations_match("Strandgata 10, 5000 Bergen", "Oslo", None))

        # Mocked API response
        fetcher = MagicMock()
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status = 200
        mock_response.retrieved_at = "2026-09-28T09:00:00Z"
        mock_response.content_sha256 = "places123"
        mock_response.json.return_value = {
            "places": [
                {
                    "title": "Acme Bygg",
                    "address": "Storgata 5, 0155 Oslo",
                    "rating": 4.5,
                    "ratingCount": 42,
                    "cid": "1234567890",
                    "category": "Byggefirma",
                    "website": "https://acmebygg.no",
                    "phoneNumber": "+47 22 00 00 00",
                }
            ]
        }
        fetcher.get.return_value = mock_response
        store = EvidenceStore()

        claims = fetch_places_ratings(
            fetcher, "999888777", "Acme Bygg AS", "Oslo", "Storgata 5", store, api_key="dummy_key"
        )
        self.assertGreaterEqual(len(claims), 4)
        rev = next(c for c in claims if c["field"] == "ratings_and_reviews")
        self.assertEqual(rev["value"]["rating"], 4.5)
        self.assertEqual(rev["value"]["rating_count"], 42)
        self.assertEqual(rev["value"]["place_id"], "1234567890")
        self.assertEqual(rev["value"]["website"], "https://acmebygg.no")
        self.assertEqual(rev["value"]["phone"], "+47 22 00 00 00")

        web_claim = next(c for c in claims if c["field"] == "website_places")
        self.assertEqual(web_claim["value"], "https://acmebygg.no")

    def test_smart_places_gating_and_website_discovery_suppression(self):
        from signalpost.sources.places import should_query_places
        from signalpost.discovery import discover

        # Holding companies and passive shells should NOT query Places (saves Serper credits)
        self.assertFalse(should_query_places({"name": "ALPHA HOLDING AS", "legal_form": "AS", "employees": 0, "industry_code": "64.200"}))
        self.assertFalse(should_query_places({"name": "BETA INVEST AS", "legal_form": "AS", "employees": 1, "industry_code": "64.300"}))
        self.assertFalse(should_query_places({"name": "SOLSTRAND EIENDOM AS", "legal_form": "AS", "employees": 0, "industry_code": "68.200"}))
        self.assertFalse(should_query_places({"name": "BORETTSLAGET OLA NORDMANN", "legal_form": "BRL", "employees": 0}))
        self.assertFalse(should_query_places({"name": "KONKURS AS", "legal_form": "AS", "bankrupt": True, "employees": 5}))

        # Storefronts, restaurants, and active employers SHOULD query Places
        self.assertTrue(should_query_places({"name": "GRAND CAFE AS", "legal_form": "AS", "employees": 12, "industry_code": "56.101"}))
        self.assertTrue(should_query_places({"name": "NORDIC HOTEL AS", "legal_form": "AS", "employees": 25, "industry_code": "55.100"}))
        self.assertTrue(should_query_places({"name": "TANDLEGE HANSEN AS", "legal_form": "AS", "employees": 3, "industry_code": "86.230"}))
        self.assertTrue(should_query_places({"name": "OSLO TANNKLINIKK AS", "legal_form": "AS", "employees": 0, "industry_code": "86.230"}))
        self.assertTrue(should_query_places({"name": "ACTIVE OPERATING AS", "legal_form": "AS", "employees": 4, "industry_code": "70.220"}))

        # Discovery incorporates place_website as top candidate and suppresses web search
        profile = {
            "organisation_number": "999111222",
            "name": "Boreal Travel AS",
            "legal_form": "AS",
            "place_website": "https://borealtravel.no",
        }
        res = discover(profile, known_domains={})
        origins = [c.origin for c in res.candidates]
        self.assertIn("google_places", origins)
        cand = next(c for c in res.candidates if c.origin == "google_places")
        self.assertEqual(cand.url, "https://borealtravel.no")

    def test_youtube_channel_and_cadence(self):
        from unittest.mock import MagicMock
        from signalpost.sources.youtube import fetch_youtube_activity, extract_channel_id_from_url_or_html
        from signalpost.evidence import EvidenceStore

        cid = extract_channel_id_from_url_or_html("https://www.youtube.com/channel/UCwyLglaZ7FUVAIZTBYvgC8w")
        self.assertEqual(cid, "UCwyLglaZ7FUVAIZTBYvgC8w")

        fetcher = MagicMock()
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status = 200
        mock_response.retrieved_at = "2026-09-28T09:00:00Z"
        mock_response.content_sha256 = "yt123"
        mock_response.text = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <author><name>Acme Channel</name></author>
  <entry>
    <title>Acme News 2026</title>
    <published>2026-09-20T10:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=xyz123"/>
  </entry>
</feed>"""
        fetcher.get.return_value = mock_response
        store = EvidenceStore()

        claims = fetch_youtube_activity(
            fetcher, "999888777", "Acme AS", "https://www.youtube.com/channel/UCwyLglaZ7FUVAIZTBYvgC8w", store
        )
        self.assertGreaterEqual(len(claims), 1)
        buzz = next(c for c in claims if c["field"] == "buzz_or_engagement")
        self.assertEqual(buzz["value"]["channel_title"], "Acme Channel")
        self.assertEqual(buzz["value"]["recent_videos_count"], 1)
        self.assertEqual(buzz["value"]["latest_video_title"], "Acme News 2026")

    def test_nav_jobs_html_parsing_and_zero_handling(self):
        from unittest.mock import MagicMock
        from signalpost.sources.nav import fetch_nav_jobs, names_match
        from signalpost.evidence import EvidenceStore

        # Verify name matching
        self.assertTrue(names_match("Acme Consulting AS", "Acme Consulting AS"))
        self.assertTrue(names_match("Acme Consulting AS", "Acme Consulting"))
        self.assertTrue(names_match("Acme Bygg AS", "Acme Bygg Avd Bergen"))
        self.assertFalse(names_match("Acme Bygg AS", "Different Bygg AS"))

        fetcher = MagicMock()
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status = 200
        mock_response.is_html = True
        mock_response.retrieved_at = "2026-09-28T09:00:00Z"
        mock_response.content_sha256 = "navhtml123"
        mock_response.text = """
        <html><body>
          <article>
            <a href="/stillinger/stilling/11111111-2222-3333-4444-555555555555">Senior Cloud Architect</a>
            <span>24. september 2026</span>
            <span>Arbeidsgiver</span>
            <span>Acme Consulting AS</span>
            <span>Sted</span>
            <span>Oslo</span>
          </article>
          <article>
            <a href="/stillinger/stilling/99999999-8888-7777-6666-555555555555">Other Job</a>
            <span>Arbeidsgiver</span>
            <span>Unrelated AS</span>
          </article>
        </body></html>
        """
        fetcher.get.return_value = mock_response
        store = EvidenceStore()

        claims = fetch_nav_jobs(fetcher, "999888777", "Acme Consulting AS", store)
        c_map = {c["field"]: c for c in claims}

        self.assertIn("active_job_count", c_map)
        self.assertEqual(c_map["active_job_count"]["value"], 1)
        self.assertIn("job_posting", c_map)
        self.assertEqual(c_map["job_posting"]["value"]["title"], "Senior Cloud Architect")
        self.assertEqual(c_map["job_posting"]["value"]["location"], "Oslo")
        self.assertEqual(c_map["job_posting"]["value"]["date_posted"], "2026-09-24")
        self.assertIn("hiring_or_activity_signal", c_map)

    def test_sentiment_model_chain_and_fallback(self):
        from signalpost.sources.sentiment import SENTIMENT_MODELS, REPORT_MODELS, rule_based_classify
        
        # Verify user model configuration: 3.7 flash for sentiment, 3.5 flash lite for report, 3.1 flash lite as fallback
        self.assertEqual(SENTIMENT_MODELS[0], "gemini-3.7-flash")
        self.assertEqual(REPORT_MODELS[0], "gemini-3.5-flash-lite")
        self.assertIn("gemini-3.1-flash-lite", SENTIMENT_MODELS)
        self.assertIn("gemini-3.1-flash-lite", REPORT_MODELS)

        # Verify deterministic rule fallback
        sent, conf, rsn = rule_based_classify("Selskapet opplever kraftig inntektsvekst og overskudd")
        self.assertEqual(sent, "positive")
        self.assertGreaterEqual(conf, 0.8)

        sent, conf, rsn = rule_based_classify("Selskapet varsler oppsigelser og stort underskudd")
        self.assertEqual(sent, "negative")
        self.assertGreaterEqual(conf, 0.8)

        sent, conf, rsn = rule_based_classify("Årsmøte avholdt i Oslo")
        self.assertEqual(sent, "neutral")

    def test_research_agent_qa_and_citations(self):
        from signalpost.research import answer_profile, screen_profiles
        from signalpost.workspace import empty_workspace, record_screen

        mock_envelope = {
            "organisation_number": "928057798",
            "input_name": "OTTEM GJENVINNING AS",
            "claims": [
                {"field": "legal_name", "value": "OTTEM GJENVINNING AS", "availability": "available", "evidence_ids": ["ev-1"]},
                {"field": "organisation_form", "value": {"code": "AS"}, "availability": "available", "evidence_ids": ["ev-1"]},
                {"field": "employees", "value": 12, "availability": "available", "evidence_ids": ["ev-1"]},
                {"field": "registered_office_municipality", "value": "SUNNDAL", "availability": "available", "evidence_ids": ["ev-1"]},
                {"field": "annual_turnover_nok", "value": {"amount": 32808480.0, "currency": "NOK"}, "availability": "available", "evidence_ids": ["ev-2"], "reporting_period": "2026-03-10"},
                {"field": "operating_profit_nok", "value": {"amount": -2883011.0, "currency": "NOK"}, "availability": "available", "evidence_ids": ["ev-2"]},
            ],
            "evidence": [
                {"id": "ev-1", "source_url": "https://data.brreg.no/1", "source_class": "official_registry", "retrieved_at": "2026-09-28T12:00:00Z", "content_sha256": "sha1"},
                {"id": "ev-2", "source_url": "https://data.brreg.no/2", "source_class": "official_annual_accounts", "retrieved_at": "2026-09-28T12:00:00Z", "content_sha256": "sha2"},
            ]
        }

        # Test single-company QA
        res = answer_profile(mock_envelope, "What are the accounts and revenue?")
        self.assertEqual(res["organisation_number"], "928057798")
        self.assertTrue(len(res["facts"]) >= 5)
        for f in res["facts"]:
            self.assertTrue(f.get("source_url"))
            self.assertTrue(f.get("retrieved_at"))
            self.assertTrue(f.get("content_sha256"))

        # Test screening query
        screen_res = screen_profiles([mock_envelope], "companies with revenue above 10 million")
        self.assertFalse(screen_res["abstained"])
        self.assertEqual(len(screen_res["results"]), 1)
        self.assertEqual(screen_res["results"][0]["organisation_number"], "928057798")

        # Test unsupported screening query abstention
        unsupported = screen_profiles([mock_envelope], "rank companies by review popularity and buzz")
        self.assertTrue(unsupported["abstained"])

        # Test workspace recording
        ws = empty_workspace()
        ws = record_screen(ws, screen_res, pin_organisations=["928057798"])
        self.assertEqual(len(ws["history"]), 1)
        self.assertIn("928057798", ws["pins"])


if __name__ == "__main__":
    unittest.main()




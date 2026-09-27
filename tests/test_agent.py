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

        # Partial overlap (2/3 tokens) with locality reaches review (0.82) safely without publishing
        signals_partial = IdentitySignals(
            title="Nordfjord Bygg",
            body_text="Vi holder til i Stryn og tilbyr snekkerarbeid.",
        )
        verdict_partial = assess_identity(profile, signals_partial, source_url="https://nordfjordbygg.no")
        self.assertFalse(verdict_partial.publishable)
        self.assertEqual(verdict_partial.status, "review")
        self.assertEqual(verdict_partial.score, 0.82)

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
        profile = {"organisation_number": "915512992", "name": "CAMFIL NORGE AS", "legal_form": "AS", "municipality": "OSLO"}
        signals = IdentitySignals(
            hostname="camfil.com",
            title="Camfil Norge | Ren luft for alle",
            body_text="Camfil Norge leverer renluftsløsninger for bygg og industri. Personvern policy Informasjonskapselpolicy. Kontakt oss."
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
        }
        signals = IdentitySignals(
            hostname="gullsmedhuset.no",
            title="Velkommen til Gullsmedhuset | Gullsmedhuset",
            body_text="Velkommen til vår butikk. Gullsmedhuset Bjørn Engebretsen leverer unike smykker. Kontakt oss på engebretsen@gullsmedhuset.no.",
        )
        verdict = assess_identity(
            profile, signals, source_url="https://gullsmedhuset.no", origin="registry"
        )
        self.assertTrue(verdict.publishable)
        self.assertGreaterEqual(verdict.score, 0.90)
        self.assertIn("bjorn", verdict.matched_tokens)
        self.assertIn("engebretsen", verdict.matched_tokens)


if __name__ == "__main__":
    unittest.main()


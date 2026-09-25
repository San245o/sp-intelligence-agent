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


if __name__ == "__main__":
    unittest.main()

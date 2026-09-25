"""Signalpost: deterministic Norwegian company-intelligence agent.

Public entry point is `pipeline.research_company`; everything else is a
connector, extractor or scoring helper it composes. No cloud LLM is used
anywhere in this package — identity is decided by deterministic evidence and the
only learned component is a local, calibrated candidate-domain ranker (a prior
and a fetch order, never an identity decision).
"""
from __future__ import annotations

__version__ = "1.0.0"

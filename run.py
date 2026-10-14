#!/usr/bin/env python3
"""Signalpost intelligence agent entrypoint.

Runs live batch entity resolution and emits OUTPUT_CONTRACT.md compliant terminal envelopes.

Usage:
    python run.py --manifest <batch_file> --out <output_dir>
    python run.py --organisations <batch_file> --output <output_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.run_batch import main

if __name__ == "__main__":
    raise SystemExit(main())

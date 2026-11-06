"""Content-addressed captures for reproducible identity and claim audits."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


DEFAULT_SOURCE_DIR = Path(__file__).resolve().parents[2] / "data" / "source-captures"


def capture_source(body: bytes, directory: Path = DEFAULT_SOURCE_DIR) -> dict[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + ".bin")
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix="capture-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {"snapshot_sha256": digest, "snapshot_path": str(target.resolve())}

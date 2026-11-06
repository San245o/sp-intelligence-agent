"""Optional private local credentials. Public source contains no credential payload."""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import zlib

PRIVATE_CONFIG = Path(__file__).resolve().parents[2] / '.local' / 'provider-credentials.json'


def load_defaults() -> list[str]:
    try:
        settings = json.loads(PRIVATE_CONFIG.read_text(encoding='utf-8'))
        values = settings.get('keys')
        if values is None:
            data = base64.b85decode(settings['payload'].encode())
            salt, packed = data[:16], data[16:]
            stream = b''.join(hashlib.sha256(b'signalpost-public-defaults-v1' + salt + i.to_bytes(4, 'big')).digest()
                              for i in range((len(packed) + 31) // 32))
            values = json.loads(zlib.decompress(bytes(a ^ b for a,b in zip(packed,stream))))
        if not isinstance(values,list) or not all(isinstance(value,str) and value.strip() for value in values):
            return []
        return list(dict.fromkeys(values))
    except (OSError,KeyError,ValueError,TypeError,zlib.error):
        return []

"""Signed evidence packs (WP v1.5).

Digest over the pack manifest; verification recomputes. This is tamper-evidence,
not a signature — actual signing still needs a certificate (v1.0 gate).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

__all__ = ["sign_evidence_pack", "verify_evidence_pack"]


def _manifest_digest(pack: Path) -> str:
    digest = hashlib.sha256()
    with zipfile.ZipFile(pack) as zf:
        for name in sorted(zf.namelist()):
            digest.update(name.encode())
            digest.update(zf.read(name))
    return digest.hexdigest()


def sign_evidence_pack(pack: str | Path) -> dict[str, Any]:
    """Append ``signature.json`` (digest manifest) into the pack in place."""
    pack = Path(pack)
    digest = _manifest_digest(pack)
    payload = {"format": "quantsmind-evidence-signature/1", "digest": digest}
    with zipfile.ZipFile(pack, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("signature.json", json.dumps(payload, indent=2))
    return payload


def verify_evidence_pack(pack: str | Path) -> bool:
    """Recompute and compare against the embedded signature."""
    pack = Path(pack)
    try:
        with zipfile.ZipFile(pack) as zf:
            sig = json.loads(zf.read("signature.json"))
            expected = str(sig.get("digest", ""))
            names = [n for n in zf.namelist() if n != "signature.json"]
            digest = hashlib.sha256()
            for name in sorted(names):
                digest.update(name.encode())
                digest.update(zf.read(name))
            return digest.hexdigest() == expected
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile):
        return False

"""TOTP MFA for admin accounts. Optional for clients/employees.

Uses pyotp for the standard RFC 6238 algorithm. Recovery codes are 10-char
url-safe random strings, hashed with bcrypt before storage.
"""
from __future__ import annotations

import json
import secrets
from base64 import b32encode
from typing import Iterable

import bcrypt
import pyotp


ISSUER = "DOD Cyber Portal"


def new_secret() -> str:
    """Fresh base32 TOTP secret (160 bits)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_email: str, issuer: str = ISSUER) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account_email, issuer_name=issuer)


def qr_svg(uri: str) -> str:
    """Render a provisioning URI as inline SVG (no Pillow dependency)."""
    import io

    import qrcode
    import qrcode.image.svg

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image()
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # qrcode renders an <?xml?> prolog we don't need when inlining.
    if svg.startswith("<?xml"):
        svg = svg.split("?>", 1)[1].strip()
    return svg


def verify_code(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def new_recovery_codes(n: int = 8) -> list[str]:
    return [secrets.token_urlsafe(8) for _ in range(n)]


def hash_recovery_codes(codes: Iterable[str]) -> list[str]:
    return [bcrypt.hashpw(c.encode(), bcrypt.gensalt(rounds=12)).decode() for c in codes]


def consume_recovery_code(stored_hashes_json: str, supplied: str) -> tuple[bool, list[str]]:
    """Return (matched, remaining_hashes_json). Each code is single-use."""
    try:
        hashes: list[str] = json.loads(stored_hashes_json or "[]")
    except (TypeError, ValueError):
        hashes = []
    supplied = (supplied or "").strip()
    if not supplied:
        return False, hashes
    for i, h in enumerate(hashes):
        try:
            if bcrypt.checkpw(supplied.encode(), h.encode()):
                # Burn the code (single-use)
                remaining = hashes[:i] + hashes[i + 1:]
                return True, remaining
        except (ValueError, TypeError):
            continue
    return False, hashes

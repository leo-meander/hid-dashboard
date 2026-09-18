"""Application-level encryption for the one guest field worth encrypting.

Guest names were readable by anyone who could open the database. That stopped
being hypothetical on 2026-09-17: the production database password had been
sitting in a public GitHub repository since the initial commit, so "can reach
the database" meant "can read every guest's name". Encrypting here means a
database-only breach yields ciphertext — the key lives in the environment, not
in a column.

Only `guestName` is encrypted. `gender`, `guest_country` and `birth_year` are
aggregated with GROUP BY and would stop working if they were opaque; they are
minimised instead (birth year rather than an exact date of birth).

No key configured means names are simply not stored. That is deliberate: the
alternative — quietly falling back to plaintext — recreates the exact problem
this module exists to prevent, and a missing env var is far more likely than a
considered decision to store names in the clear.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

PREFIX = "v1:"
_NONCE_BYTES = 12


def _key() -> Optional[bytes]:
    """The 32-byte AES key from PII_ENCRYPTION_KEY, or None when unset.

    Settings first so a local .env works, then the raw environment, which is
    how Zeabur supplies it.
    """
    raw = ""
    try:
        from app.config import settings
        raw = (getattr(settings, "PII_ENCRYPTION_KEY", "") or "").strip()
    except Exception:
        pass
    if not raw:
        raw = (os.environ.get("PII_ENCRYPTION_KEY") or "").strip()
    if not raw:
        return None
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception:
        logger.error("PII_ENCRYPTION_KEY is not valid base64 — names will not be stored")
        return None
    if len(key) != 32:
        logger.error(
            "PII_ENCRYPTION_KEY decodes to %d bytes, need 32 — names will not be stored",
            len(key),
        )
        return None
    return key


def is_configured() -> bool:
    return _key() is not None


def generate_key() -> str:
    """A fresh base64 key, for putting in the environment. Never store it in the DB."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a value, or None when there is nothing to store or no key."""
    if plaintext is None:
        return None
    text = str(plaintext).strip()
    if not text:
        return None
    key = _key()
    if key is None:
        return None
    nonce = os.urandom(_NONCE_BYTES)
    blob = nonce + AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(blob).decode()


def decrypt(value: Optional[str]) -> Optional[str]:
    """Decrypt a stored value.

    A value without the version prefix is returned unchanged: while the backfill
    is still walking the table some rows are encrypted and some are not, and a
    half-migrated table should still render rather than show nulls.
    """
    if not value:
        return None
    if not value.startswith(PREFIX):
        return value
    key = _key()
    if key is None:
        return None
    try:
        blob = base64.urlsafe_b64decode(value[len(PREFIX):])
        return AESGCM(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], None).decode("utf-8")
    except (InvalidTag, ValueError, TypeError):
        logger.warning("Could not decrypt a guest name — wrong or rotated key?")
        return None

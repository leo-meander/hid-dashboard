"""Guest-name encryption, and the one behaviour that matters most: never
falling back to plaintext when the key is missing."""
import base64
import importlib
import os

import pytest

from app.services import pii_crypto
from app.services.webhook_log import mask_email


@pytest.fixture
def key(monkeypatch):
    k = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("PII_ENCRYPTION_KEY", k)
    monkeypatch.setattr("app.config.settings.PII_ENCRYPTION_KEY", k, raising=False)
    importlib.reload(pii_crypto)
    yield k
    importlib.reload(pii_crypto)


def test_a_name_survives_a_round_trip(key):
    assert pii_crypto.decrypt(pii_crypto.encrypt("Ariel Siegelman")) == "Ariel Siegelman"


def test_ciphertext_does_not_contain_the_name(key):
    blob = pii_crypto.encrypt("Ariel Siegelman")
    assert "Ariel" not in blob and "Siegelman" not in blob
    assert blob.startswith("v1:")


def test_the_same_name_encrypts_differently_each_time(key):
    """A fixed nonce would let anyone group bookings by guest without the key."""
    assert pii_crypto.encrypt("Ariel Siegelman") != pii_crypto.encrypt("Ariel Siegelman")


def test_no_key_means_no_storage_rather_than_plaintext(monkeypatch):
    """The failure this whole module exists to prevent.

    A missing env var is far more likely than a decision to store names in the
    clear, so an unset key must not quietly write the name anyway.
    """
    monkeypatch.delenv("PII_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr("app.config.settings.PII_ENCRYPTION_KEY", "", raising=False)
    importlib.reload(pii_crypto)
    assert pii_crypto.is_configured() is False
    assert pii_crypto.encrypt("Ariel Siegelman") is None
    importlib.reload(pii_crypto)


def test_blank_and_missing_names_store_nothing(key):
    assert pii_crypto.encrypt(None) is None
    assert pii_crypto.encrypt("   ") is None


def test_a_wrong_key_returns_none_instead_of_raising(key, monkeypatch):
    """A rotated key must not take the whole endpoint down with it."""
    blob = pii_crypto.encrypt("Ariel Siegelman")
    other = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("PII_ENCRYPTION_KEY", other)
    monkeypatch.setattr("app.config.settings.PII_ENCRYPTION_KEY", other, raising=False)
    importlib.reload(pii_crypto)
    assert pii_crypto.decrypt(blob) is None


def test_unprefixed_values_pass_through_for_the_half_migrated_table(key):
    """Mid-backfill some rows are ciphertext and some are not; both must render."""
    assert pii_crypto.decrypt("Legacy Plain Name") == "Legacy Plain Name"
    assert pii_crypto.decrypt(None) is None


def test_generated_keys_are_usable(monkeypatch):
    k = pii_crypto.generate_key()
    monkeypatch.setenv("PII_ENCRYPTION_KEY", k)
    monkeypatch.setattr("app.config.settings.PII_ENCRYPTION_KEY", k, raising=False)
    importlib.reload(pii_crypto)
    assert pii_crypto.decrypt(pii_crypto.encrypt("Nguyen Van A")) == "Nguyen Van A"
    importlib.reload(pii_crypto)


# ── Webhook Monitor email masking ────────────────────────────────────────────

def test_email_is_masked_but_still_recognisable():
    assert mask_email("ariel@example.com") == "ar***@example.com"


def test_masking_leaves_nothing_useful_on_odd_input():
    assert mask_email("notanemail") == "***"
    assert mask_email("@example.com") == "***@example.com"


def test_masking_passes_empty_values_through():
    assert mask_email(None) is None
    assert mask_email("") == ""

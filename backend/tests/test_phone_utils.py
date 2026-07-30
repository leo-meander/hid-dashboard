"""Tests for services/phone_utils.py."""
from app.services.phone_utils import normalize_e164, normalize_e164_digits


class TestNormalizeE164:
    def test_keeps_explicit_country_code(self):
        assert normalize_e164("+886 912 345 678", "886") == "+886912345678"

    def test_strips_national_trunk_prefix(self):
        assert normalize_e164("0912-345-678", "886") == "+886912345678"

    def test_converts_international_prefix(self):
        assert normalize_e164("00886912345678", "886") == "+886912345678"

    def test_prepends_branch_country_for_bare_local_number(self):
        assert normalize_e164("912345678", "886") == "+886912345678"

    def test_does_not_double_prepend_country_code(self):
        assert normalize_e164("886912345678", "886") == "+886912345678"

    def test_uses_branch_country_per_market(self):
        assert normalize_e164("090-1234-5678", "81") == "+819012345678"
        assert normalize_e164("0912345678", "84") == "+84912345678"

    def test_strips_formatting_characters(self):
        assert normalize_e164("(0912) 345.678", "886") == "+886912345678"

    def test_rejects_too_short_and_empty(self):
        assert normalize_e164("123", "886") is None
        assert normalize_e164("", "886") is None
        assert normalize_e164(None, "886") is None

    def test_returns_none_when_no_country_code_available(self):
        assert normalize_e164("912345678", "") is None

    def test_plus_prefixed_number_needs_no_country_code(self):
        assert normalize_e164("+886912345678", "") == "+886912345678"


class TestNormalizeE164Digits:
    def test_drops_leading_plus(self):
        assert normalize_e164_digits("+886 912 345 678", "886") == "886912345678"

    def test_still_adds_missing_country_code(self):
        assert normalize_e164_digits("0912-345-678", "886") == "886912345678"

    def test_propagates_none(self):
        assert normalize_e164_digits("123", "886") is None
        assert normalize_e164_digits(None, "886") is None

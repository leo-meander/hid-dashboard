"""Tests for services/email_utils.py — OTA alias email rejection."""
from app.services.email_utils import usable_email


class TestUsableEmail:
    def test_real_address_is_normalized_to_lowercase(self):
        assert usable_email("  Guest@Example.COM ") == "guest@example.com"

    def test_missing_address_is_unusable(self):
        assert usable_email("") is None
        assert usable_email("   ") is None
        assert usable_email(None) is None

    def test_cloudbeds_placeholder_is_unusable(self):
        assert usable_email("N/A") is None
        assert usable_email("n/a@guest.com") is None

    def test_placeholder_check_does_not_reject_real_addresses(self):
        # "N/A" as a substring, not a word-boundary match — na@ is a real inbox
        assert usable_email("na@company.com") == "na@company.com"
        assert usable_email("hana@company.com") == "hana@company.com"

    def test_ota_alias_domains_are_unusable(self):
        assert usable_email("i5x9@guest.ctrip.com") is None
        assert usable_email("i5x9@guest.trip.com") is None
        assert usable_email("abc@guest.booking.com") is None

    def test_alias_check_is_case_insensitive(self):
        assert usable_email("I5X9@Guest.Ctrip.COM") is None

    def test_ota_corporate_domain_is_still_usable(self):
        # Only the per-booking relay subdomains are aliases; a human at the OTA
        # writing from the corporate domain is a real address.
        assert usable_email("partner@ctrip.com") == "partner@ctrip.com"

    def test_malformed_addresses_are_unusable(self):
        assert usable_email("not-an-email") is None
        assert usable_email("two@@example.com") is None
        assert usable_email("no-tld@localhost") is None

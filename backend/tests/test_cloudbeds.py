"""Tests for services/cloudbeds.py mapping logic."""
import pytest
from app.services.cloudbeds import (
    map_country_code,
    map_room_type_category,
    map_source_category,
    normalize_source,
    _parse_date,
)


class TestMapCountryCode:
    def test_known_aliases(self):
        # Full-name aliases canonicalise to the standard display name.
        assert map_country_code("United States of America") == "United States"
        assert map_country_code("Viet Nam") == "Vietnam"
        assert map_country_code("Korea, Republic of") == "South Korea"

    def test_iso_codes(self):
        # 2-letter ISO codes map to full names.
        assert map_country_code("JP") == "Japan"
        assert map_country_code("VN") == "Vietnam"
        assert map_country_code("tw") == "Taiwan"  # case-insensitive

    def test_passthrough(self):
        # Recognised but un-aliased strings pass through unchanged so we
        # don't lose granularity for less-common countries.
        assert map_country_code("Australia") == "Australia"
        assert map_country_code("Vietnam") == "Vietnam"
        assert map_country_code("Bhutan") == "Bhutan"

    def test_literal_unknown_passes_through(self):
        # Per current semantics: "Unknown" sentinel is reserved for missing
        # data. If Cloudbeds ever sends the literal string "Unknown", treat
        # it the same way (i.e. preserve as "Unknown").
        assert map_country_code("Unknown") == "Unknown"

    def test_none_or_empty_returns_unknown(self):
        # Missing data → "Unknown" (NOT "Others" — "Others" is reserved for
        # a future change covering "country present but unrecognised").
        assert map_country_code(None) == "Unknown"
        assert map_country_code("") == "Unknown"
        assert map_country_code("   ") == "Unknown"


class TestMapRoomTypeCategory:
    def test_dorm_variants(self):
        assert map_room_type_category("Dorm Bed") == "Dorm"
        assert map_room_type_category("Mixed DORM 6-bed") == "Dorm"
        assert map_room_type_category("female dorm") == "Dorm"

    def test_room_variants(self):
        assert map_room_type_category("Deluxe Double Room") == "Room"
        assert map_room_type_category("Standard Twin") == "Room"
        assert map_room_type_category(None) == "Room"

    def test_edge_case_empty(self):
        assert map_room_type_category("") == "Room"


class TestMapSourceCategory:
    def test_direct_keywords(self):
        assert map_source_category("Hotel Website") == "Direct"
        assert map_source_category("Booking Engine") == "Direct"
        assert map_source_category("Direct") == "Direct"
        assert map_source_category("Travel Blogger") == "Direct"
        assert map_source_category("Walk-In") == "Direct"
        assert map_source_category("Walk In") == "Direct"
        assert map_source_category("Extension") == "Direct"
        assert map_source_category("Phone") == "Direct"
        assert map_source_category("Email") == "Direct"
        assert map_source_category("Facebook") == "Direct"
        assert map_source_category("Public Relations") == "Direct"

    def test_ota(self):
        assert map_source_category("Booking.com") == "OTA"
        assert map_source_category("Hostelworld") == "OTA"
        assert map_source_category("Agoda") == "OTA"
        assert map_source_category("Ctrip") == "OTA"
        assert map_source_category("Traveloka") == "OTA"

    def test_local_travel_agency(self):
        # Vietnamese corporate clients
        assert map_source_category("CÔNG TY TNHH Daichi Jitsuqyo Việt Nam") == "Local travel agency"
        assert map_source_category("Cong Ty TNHH ABC") == "Local travel agency"
        # English corporate / agency
        assert map_source_category("Acme Co., Ltd") == "Local travel agency"
        assert map_source_category("Global Travel Agency") == "Local travel agency"
        assert map_source_category("Saigon Travel Agent") == "Local travel agency"
        assert map_source_category("Some Corporate Account") == "Local travel agency"
        # CJK corporate suffixes
        assert map_source_category("テスト株式会社") == "Local travel agency"
        assert map_source_category("測試有限公司") == "Local travel agency"

    def test_direct_beats_local_ta(self):
        # "Blogger" is Direct even if "agency" also appears
        assert map_source_category("Travel Blogger Agency") == "Direct"

    def test_none_or_empty(self):
        assert map_source_category(None) == "OTA"
        assert map_source_category("") == "OTA"


class TestNormalizeSource:
    def test_canonical_ota(self):
        assert normalize_source("Booking.com Rates") == "Booking.com"
        assert normalize_source("hostelworld special") == "Hostelworld"
        assert normalize_source("Trip.com") == "Ctrip"

    def test_unknown_source_passthrough(self):
        assert normalize_source("SomeOtherOTA") == "SomeOtherOTA"

    def test_none(self):
        assert normalize_source(None) is None


class TestParseDate:
    def test_valid_date(self):
        from datetime import date
        assert _parse_date("2025-06-15") == date(2025, 6, 15)

    def test_datetime_string_truncated(self):
        from datetime import date
        assert _parse_date("2025-06-15T10:30:00") == date(2025, 6, 15)

    def test_none_or_empty(self):
        assert _parse_date(None) is None
        assert _parse_date("") is None

    def test_invalid_format(self):
        assert _parse_date("not-a-date") is None

"""
Guards that Meta CAPI hashes each user_data field in Meta's normalized shape.

Meta normalizes its own copy before hashing, so a field hashed in any other
shape misses every time — "1990-01-15" and "19900115" are simply two different
hashes. These tests pin the normalization that the raw Cloudbeds values need.
"""
import hashlib
from unittest.mock import MagicMock, patch

from app.services.meta_capi_service import send_purchase_event


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _client():
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "{}"
    resp.json.return_value = {"events_received": 1}
    client = MagicMock()
    client.post.return_value = resp
    client.__enter__ = lambda s: client
    client.__exit__ = lambda *a: False
    return client


def _send(**guest_fields) -> dict:
    """Send one reservation and return the user_data Meta received."""
    guest = {"guestPhone": "0912345678", **guest_fields}
    reservation = {
        "reservationID": "R-1",
        "dateCreated": "2026-07-30 10:21:00",
        "total": "1500",
        "guestEmail": "guest@example.com",
        "guestList": {"g1": guest},
    }
    client = _client()
    with patch("httpx.Client", return_value=client):
        send_purchase_event(
            reservation, pixel_id="PX", access_token="tok", phone_country_code="84"
        )
    return client.post.call_args.kwargs["json"]["data"][0]["user_data"]


class TestBirthdate:
    def test_iso_date_becomes_yyyymmdd(self):
        assert _send(guestBirthdate="1990-01-15")["db"] == _h("19900115")

    def test_key_spelled_with_capital_d_also_read(self):
        assert _send(guestBirthDate="1990-01-15")["db"] == _h("19900115")

    def test_datetime_value_keeps_only_the_date(self):
        assert _send(guestBirthdate="1990-01-15 00:00:00")["db"] == _h("19900115")

    def test_empty_or_partial_value_omits_the_field(self):
        assert "db" not in _send(guestBirthdate="")
        assert "db" not in _send(guestBirthdate="1990")


class TestCityStateZip:
    def test_city_loses_spaces(self):
        assert _send(guestCity="Ho Chi Minh")["ct"] == _h("hochiminh")

    def test_state_loses_spaces(self):
        assert _send(guestState="Ho Chi Minh City")["st"] == _h("hochiminhcity")

    def test_zip_loses_spaces_and_punctuation(self):
        assert _send(guestZip="700 000")["zp"] == _h("700000")
        assert _send(guestZip="70-000")["zp"] == _h("70000")

    def test_accents_are_preserved_not_folded(self):
        assert _send(guestCity="Hồ Chí Minh")["ct"] == _h("hồchíminh")

    def test_punctuation_only_value_omits_the_field(self):
        assert "ct" not in _send(guestCity="--")


class TestGender:
    def test_cloudbeds_single_letter_is_lowercased(self):
        assert _send(guestGender="M")["ge"] == _h("m")
        assert _send(guestGender="F")["ge"] == _h("f")

    def test_spelled_out_value_maps_to_single_letter(self):
        assert _send(guestGender="Female")["ge"] == _h("f")

    def test_placeholder_is_not_hashed_as_an_identifier(self):
        # Cloudbeds sends the literal "N/A" when a channel supplies no gender
        assert "ge" not in _send(guestGender="N/A")

    def test_unrecognised_value_omits_the_field(self):
        assert "ge" not in _send(guestGender="unknown")


class TestUntouchedFields:
    def test_country_stays_two_letter_lowercase(self):
        assert _send(guestCountry="VN")["country"] == _h("vn")

    def test_names_keep_their_spacing_and_accents(self):
        # Meta's SDK strips names to a-z, which would mangle these
        user_data = _send(guestFirstName="Văn A", guestLastName="Nguyễn")
        assert user_data["fn"] == _h("văn a")
        assert user_data["ln"] == _h("nguyễn")

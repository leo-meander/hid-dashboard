"""
Guards that Meta CAPI and TikTok hash a country-code-qualified phone number.

Before the Data Manager migration all three conversion services stripped "+"
without ever adding a country code, so a Cloudbeds phone like "0912-345-678"
was hashed as-is and matched nothing. These tests pin the fixed behaviour.
"""
import hashlib
from unittest.mock import MagicMock, patch

from app.services.meta_capi_service import send_purchase_event
from app.services.tiktok_capi_service import send_complete_payment_event


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _reservation(phone="0912-345-678", email="guest@example.com"):
    return {
        "reservationID": "R-1",
        "dateCreated": "2026-07-30 10:21:00",
        "total": "1500",
        "source": "booking.com",
        "guestEmail": email,
        "guestList": {"g1": {"guestPhone": phone}},
    }


def _client(body):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "{}"
    resp.json.return_value = body
    client = MagicMock()
    client.post.return_value = resp
    client.__enter__ = lambda s: client
    client.__exit__ = lambda *a: False
    return client


class TestMetaPhoneNormalization:
    def test_local_number_gets_branch_country_code(self):
        client = _client({"events_received": 1})
        with patch("httpx.Client", return_value=client):
            result = send_purchase_event(
                _reservation(), pixel_id="PX", access_token="tok", phone_country_code="886"
            )

        assert result["success"] is True
        user_data = client.post.call_args.kwargs["json"]["data"][0]["user_data"]
        assert user_data["ph"] == _h("886912345678")

    def test_osaka_branch_uses_japan_country_code(self):
        client = _client({"events_received": 1})
        with patch("httpx.Client", return_value=client):
            send_purchase_event(
                _reservation(phone="090-1234-5678"),
                pixel_id="PX",
                access_token="tok",
                phone_country_code="81",
            )

        user_data = client.post.call_args.kwargs["json"]["data"][0]["user_data"]
        assert user_data["ph"] == _h("819012345678")

    def test_international_number_unchanged_apart_from_formatting(self):
        client = _client({"events_received": 1})
        with patch("httpx.Client", return_value=client):
            send_purchase_event(
                _reservation(phone="+886 912 345 678"),
                pixel_id="PX",
                access_token="tok",
                phone_country_code="886",
            )

        user_data = client.post.call_args.kwargs["json"]["data"][0]["user_data"]
        assert user_data["ph"] == _h("886912345678")

    def test_unusable_phone_omits_field_but_still_sends_email(self):
        client = _client({"events_received": 1})
        with patch("httpx.Client", return_value=client):
            result = send_purchase_event(
                _reservation(phone="123"), pixel_id="PX", access_token="tok"
            )

        user_data = client.post.call_args.kwargs["json"]["data"][0]["user_data"]
        assert result["success"] is True
        assert "ph" not in user_data
        assert user_data["em"] == _h("guest@example.com")


class TestOtaAliasEmailIsDropped:
    """OTA relay addresses are unmatchable — send the phone, drop the email."""

    def test_meta_omits_alias_email_but_keeps_phone(self):
        client = _client({"events_received": 1})
        with patch("httpx.Client", return_value=client):
            result = send_purchase_event(
                _reservation(email="i5x9@guest.ctrip.com"),
                pixel_id="PX",
                access_token="tok",
                phone_country_code="84",
            )

        user_data = client.post.call_args.kwargs["json"]["data"][0]["user_data"]
        assert result["success"] is True
        assert "em" not in user_data
        assert user_data["ph"] == _h("84912345678")

    def test_tiktok_omits_alias_email_but_keeps_phone(self):
        client = _client({"code": 0})
        with patch("httpx.Client", return_value=client):
            result = send_complete_payment_event(
                _reservation(email="i5x9@guest.trip.com"),
                access_token="tok",
                event_source_id="ES",
            )

        user = client.post.call_args.kwargs["json"]["data"][0]["user"]
        assert result["success"] is True
        assert "email" not in user
        assert user["phone_number"] == [_h("84912345678")]

    def test_tiktok_skips_when_alias_email_is_the_only_identifier(self):
        client = _client({"code": 0})
        with patch("httpx.Client", return_value=client):
            result = send_complete_payment_event(
                _reservation(email="i5x9@guest.ctrip.com", phone=""),
                access_token="tok",
                event_source_id="ES",
            )

        assert result == {"success": False, "error": "no_user_data"}
        client.post.assert_not_called()


class TestTikTokPhoneNormalization:
    def test_local_number_gets_vietnam_country_code(self):
        client = _client({"code": 0})
        with patch("httpx.Client", return_value=client):
            result = send_complete_payment_event(
                _reservation(phone="0912345678"), access_token="tok", event_source_id="ES"
            )

        assert result["success"] is True
        user = client.post.call_args.kwargs["json"]["data"][0]["user"]
        assert user["phone_number"] == [_h("84912345678")]

    def test_nonzero_response_code_is_a_failure(self):
        client = _client({"code": 40001, "message": "invalid event_source_id"})
        with patch("httpx.Client", return_value=client):
            result = send_complete_payment_event(
                _reservation(), access_token="tok", event_source_id="ES"
            )

        assert result["success"] is False
        assert result["response"]["message"] == "invalid event_source_id"

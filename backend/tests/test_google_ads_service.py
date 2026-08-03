"""Tests for services/google_ads_service.py (Data Manager API)."""
import hashlib
from unittest.mock import MagicMock, patch

from app.services.google_ads_service import (
    _parse_conversion_time,
    _sha256,
    upload_offline_conversion,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class TestParseConversionTime:
    def test_returns_rfc3339_utc(self):
        # Taipei 10:21 local, minus tz offset 8 and Make's extra hour
        assert _parse_conversion_time("2026-07-30 10:21:00", 8, 1) == "2026-07-30T01:21:00Z"

    def test_saigon_has_no_extra_offset(self):
        assert _parse_conversion_time("2026-07-30 10:21:00", 7, 0) == "2026-07-30T03:21:00Z"

    def test_invalid_input_returns_none(self):
        assert _parse_conversion_time("not-a-date") is None
        assert _parse_conversion_time(None) is None


class TestUploadOfflineConversion:
    BASE_KWARGS = dict(
        customer_id="123-456-7890",
        client_id="cid",
        client_secret="secret",
        refresh_token="refresh",
        conversion_action_single="1111",
        conversion_action_both="2222",
        login_customer_id="999-888-7777",
        currency="TWD",
        tz_offset_hours=8,
        event_time_extra_offset=1,
        phone_country_code="886",
    )

    def _reservation(self, email="Guest@Example.com", phone="0912-345-678"):
        return {
            "reservationID": "R-1",
            "dateCreated": "2026-07-30 10:21:00",
            "total": "1500.5",
            "guestEmail": email,
            "guestList": {"g1": {"guestPhone": phone}},
        }

    def _post(self, status_code=200, body=None):
        """Patch token exchange + ingest POST, returning the ingest mock."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = "{}"
        resp.json.return_value = body if body is not None else {"requestId": "req-1"}
        client = MagicMock()
        client.post.return_value = resp
        client.__enter__ = lambda s: client
        client.__exit__ = lambda *a: False
        return client

    def test_both_identifiers_uses_both_action(self):
        client = self._post()
        with patch("app.services.google_ads_service._get_access_token", return_value="tok"), \
             patch("httpx.Client", return_value=client):
            result = upload_offline_conversion(self._reservation(), **self.BASE_KWARGS)

        assert result["success"] is True
        assert result["case"] == "both"
        assert result["request_id"] == "req-1"

        payload = client.post.call_args.kwargs["json"]
        dest = payload["destinations"][0]
        assert dest["operatingAccount"] == {"accountType": "GOOGLE_ADS", "accountId": "1234567890"}
        assert dest["loginAccount"] == {"accountType": "GOOGLE_ADS", "accountId": "9998887777"}
        assert dest["productDestinationId"] == "2222"

        event = payload["events"][0]
        assert event["transactionId"] == "R-1"
        assert event["eventTimestamp"] == "2026-07-30T01:21:00Z"
        # Required by the API for offline conversions despite the schema
        # marking it optional — omitting it is a hard 400.
        assert event["eventSource"] == "OTHER"
        assert event["conversionValue"] == 1500.5
        assert event["currency"] == "TWD"
        assert payload["encoding"] == "HEX"
        assert payload["validateOnly"] is False

        # Email lowercased before hashing; phone hashed in E.164 form
        assert event["userData"]["userIdentifiers"] == [
            {"emailAddress": _h("guest@example.com")},
            {"phoneNumber": _h("+886912345678")},
        ]

    def test_email_only_uses_single_action(self):
        client = self._post()
        with patch("app.services.google_ads_service._get_access_token", return_value="tok"), \
             patch("httpx.Client", return_value=client):
            result = upload_offline_conversion(self._reservation(phone=""), **self.BASE_KWARGS)

        payload = client.post.call_args.kwargs["json"]
        assert result["case"] == "email_only"
        assert payload["destinations"][0]["productDestinationId"] == "1111"
        assert payload["events"][0]["userData"]["userIdentifiers"] == [
            {"emailAddress": _h("guest@example.com")}
        ]

    def test_phone_only_falls_back_to_single_action(self):
        client = self._post()
        with patch("app.services.google_ads_service._get_access_token", return_value="tok"), \
             patch("httpx.Client", return_value=client):
            result = upload_offline_conversion(self._reservation(email="N/A"), **self.BASE_KWARGS)

        payload = client.post.call_args.kwargs["json"]
        assert result["case"] == "phone_only"
        assert payload["destinations"][0]["productDestinationId"] == "1111"

    def test_ota_alias_email_routes_to_phone_only(self):
        """A Ctrip alias address must not pull the booking into the both action."""
        client = self._post()
        with patch("app.services.google_ads_service._get_access_token", return_value="tok"), \
             patch("httpx.Client", return_value=client):
            result = upload_offline_conversion(
                self._reservation(email="i5x9@guest.ctrip.com"), **self.BASE_KWARGS
            )

        payload = client.post.call_args.kwargs["json"]
        assert result["case"] == "phone_only"
        assert payload["destinations"][0]["productDestinationId"] == "1111"
        assert payload["events"][0]["userData"]["userIdentifiers"] == [
            {"phoneNumber": _h("+886912345678")}
        ]

    def test_ota_alias_email_without_phone_skips_entirely(self):
        with patch("app.services.google_ads_service._get_access_token") as token:
            result = upload_offline_conversion(
                self._reservation(email="i5x9@guest.trip.com", phone=""), **self.BASE_KWARGS
            )
        assert result == {"success": False, "case": "skipped_no_identifiers"}
        token.assert_not_called()

    def test_phone_only_uses_dedicated_action_when_set(self):
        client = self._post()
        kwargs = {**self.BASE_KWARGS, "conversion_action_phone": "3333"}
        with patch("app.services.google_ads_service._get_access_token", return_value="tok"), \
             patch("httpx.Client", return_value=client):
            upload_offline_conversion(self._reservation(email=""), **kwargs)

        payload = client.post.call_args.kwargs["json"]
        assert payload["destinations"][0]["productDestinationId"] == "3333"

    def test_no_identifiers_skips_without_calling_api(self):
        with patch("app.services.google_ads_service._get_access_token") as token:
            result = upload_offline_conversion(
                self._reservation(email="n/a", phone=""), **self.BASE_KWARGS
            )
        assert result == {"success": False, "case": "skipped_no_identifiers"}
        token.assert_not_called()

    def test_missing_conversion_action_fails_before_token_exchange(self):
        kwargs = {**self.BASE_KWARGS, "conversion_action_both": ""}
        with patch("app.services.google_ads_service._get_access_token") as token:
            result = upload_offline_conversion(self._reservation(), **kwargs)
        assert result["success"] is False
        assert result["error"] == "no_conversion_action"
        token.assert_not_called()

    def test_token_failure_reported(self):
        with patch("app.services.google_ads_service._get_access_token", return_value=None):
            result = upload_offline_conversion(self._reservation(), **self.BASE_KWARGS)
        assert result == {"success": False, "case": "both", "error": "token_refresh_failed"}

    def test_http_error_surfaces_google_message(self):
        client = self._post(
            status_code=403,
            body={"error": {"message": "Request had insufficient authentication scopes."}},
        )
        with patch("app.services.google_ads_service._get_access_token", return_value="tok"), \
             patch("httpx.Client", return_value=client):
            result = upload_offline_conversion(self._reservation(), **self.BASE_KWARGS)

        assert result["success"] is False
        assert result["status_code"] == 403
        assert "insufficient authentication scopes" in result["error"]

    def test_validate_only_flag_passed_through(self):
        client = self._post()
        with patch("app.services.google_ads_service._get_access_token", return_value="tok"), \
             patch("httpx.Client", return_value=client):
            upload_offline_conversion(self._reservation(), validate_only=True, **self.BASE_KWARGS)

        assert client.post.call_args.kwargs["json"]["validateOnly"] is True

    def test_sha256_ignores_blank_values(self):
        assert _sha256("") is None
        assert _sha256("  ") is None
        assert _sha256(None) is None

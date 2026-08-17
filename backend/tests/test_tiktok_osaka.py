"""
Guards that Osaka's TikTok offline conversions leave with Osaka's own settings.

TikTok used to be a Saigon-only branch of the fan-out, with VND, UTC+7 and +84
baked into the service. Sending Osaka through that unchanged would have posted
JPY amounts labelled VND, Japanese phone numbers prefixed 84, and a timestamp
two hours away from the one Meta and Google Ads got for the same reservation.
"""
import hashlib
from unittest.mock import MagicMock, patch

from app.config import TIKTOK_BRANCHES, settings
from app.services.tiktok_capi_service import send_complete_payment_event


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _reservation(phone="090-1234-5678", email="guest@example.com"):
    return {
        "reservationID": "R-OSK-1",
        "dateCreated": "2026-08-15 18:30:00",
        "total": "24000",
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


def _send_as_osaka(reservation):
    cfg = settings.get_webhook_config_for_branch("osaka")
    client = _client({"code": 0})
    with patch("httpx.Client", return_value=client):
        result = send_complete_payment_event(
            reservation=reservation,
            access_token="tok",
            event_source_id=cfg["tiktok_event_source_id"],
            currency=cfg["currency"],
            tz_offset_hours=cfg["tz_offset_hours"],
            event_time_extra_offset=cfg["event_time_extra_offset"],
            phone_country_code=cfg["phone_country_code"],
        )
    return result, client


class TestOsakaBranchConfig:
    def test_osaka_is_wired_for_tiktok(self):
        assert "osaka" in TIKTOK_BRANCHES

    def test_osaka_has_its_own_offline_event_set(self):
        cfg = settings.get_webhook_config_for_branch("osaka")
        # The events carry event_source="offline", so this must be the Offline
        # Event Set ID — never the web pixel (D9QKA0BC77U6RO6J1O50).
        assert cfg["tiktok_event_source_id"] == "7674850424402378773"
        assert cfg["tiktok_event_source_id"] != settings.TIKTOK_EVENT_SOURCE_ID_SAIGON

    def test_branch_without_tiktok_gets_no_credentials(self):
        for branch in ("taipei", "1948", "oani"):
            cfg = settings.get_webhook_config_for_branch(branch)
            assert cfg["tiktok_access_token"] == ""
            assert cfg["tiktok_event_source_id"] == ""


class TestOsakaPayload:
    def test_amount_is_reported_in_yen(self):
        result, client = _send_as_osaka(_reservation())

        assert result["success"] is True
        payload = client.post.call_args.kwargs["json"]
        assert payload["data"][0]["properties"]["currency"] == "JPY"
        assert payload["data"][0]["properties"]["value"] == 24000.0

    def test_local_number_gets_japan_country_code(self):
        _, client = _send_as_osaka(_reservation())

        user = client.post.call_args.kwargs["json"]["data"][0]["user"]
        assert user["phone_number"] == [_h("819012345678")]

    def test_event_time_matches_the_other_channels(self):
        from app.services.meta_capi_service import _parse_event_time as meta_time

        _, client = _send_as_osaka(_reservation())

        cfg = settings.get_webhook_config_for_branch("osaka")
        expected = meta_time(
            "2026-08-15 18:30:00",
            tz_offset_hours=cfg["tz_offset_hours"],
            extra_offset_hours=cfg["event_time_extra_offset"],
        )
        assert client.post.call_args.kwargs["json"]["data"][0]["event_time"] == expected

    def test_event_set_id_goes_out_as_the_offline_source(self):
        _, client = _send_as_osaka(_reservation())

        payload = client.post.call_args.kwargs["json"]
        assert payload["event_source"] == "offline"
        assert payload["event_source_id"] == "7674850424402378773"


class TestSaigonDefaultsUnchanged:
    """Saigon calls the service with no overrides in older tests — keep it so."""

    def test_defaults_still_describe_saigon(self):
        client = _client({"code": 0})
        with patch("httpx.Client", return_value=client):
            send_complete_payment_event(
                _reservation(phone="0912345678"),
                access_token="tok",
                event_source_id="ES",
            )

        data = client.post.call_args.kwargs["json"]["data"][0]
        assert data["properties"]["currency"] == "VND"
        assert data["user"]["phone_number"] == [_h("84912345678")]

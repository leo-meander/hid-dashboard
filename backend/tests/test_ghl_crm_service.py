"""Tests for services/ghl_crm_service.py country handling and create fallbacks."""
from unittest.mock import MagicMock

from app.services.ghl_crm_service import _clean_country, create_contact


class TestCleanCountry:
    def test_accepts_valid_iso_codes(self):
        assert _clean_country("VN") == "VN"
        assert _clean_country("tw") == "TW"
        assert _clean_country(" jp ") == "JP"

    def test_maps_known_non_iso_aliases(self):
        assert _clean_country("UK") == "GB"
        assert _clean_country("el") == "GR"

    def test_rejects_two_letter_non_iso_codes(self):
        # A length check alone let these through and GHL rejected the whole
        # create with "country must be valid".
        assert _clean_country("XX") is None
        assert _clean_country("ZZ") is None

    def test_rejects_full_names_and_empty(self):
        assert _clean_country("Vietnam") is None
        assert _clean_country("") is None
        assert _clean_country(None) is None


class TestCreateContact:
    DUP_PHONE_BODY = (
        '{"statusCode":400,"message":"This location does not allow duplicated '
        'contacts.","meta":{"contactId":"abc123","matchingField":"phone"}}'
    )

    def _resp(self, status_code, text="{}", payload=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.json.return_value = payload if payload is not None else {}
        return resp

    def test_returns_id_on_success(self):
        client = MagicMock()
        client.post.return_value = self._resp(201, payload={"contact": {"id": "c1"}})

        contact_id, err = create_contact(client, "loc1", "key", {"email": "a@b.com"})

        assert (contact_id, err) == ("c1", None)

    def test_retries_without_phone_on_duplicate(self):
        client = MagicMock()
        client.post.side_effect = [
            self._resp(400, text=self.DUP_PHONE_BODY),
            self._resp(201, payload={"contact": {"id": "c2"}}),
        ]

        contact_id, err = create_contact(
            client, "loc1", "key", {"email": "a@b.com", "phone": "+84912345678"}
        )

        assert (contact_id, err) == ("c2", None)
        assert client.post.call_count == 2
        retry_body = client.post.call_args_list[1].kwargs["json"]
        assert "phone" not in retry_body
        assert retry_body["email"] == "a@b.com"
        # locationId must survive the retry or the contact lands nowhere
        assert retry_body["locationId"] == "loc1"

    def test_reports_error_when_retry_also_fails(self):
        client = MagicMock()
        client.post.side_effect = [
            self._resp(400, text=self.DUP_PHONE_BODY),
            self._resp(422, text='{"message":["country must be valid"]}'),
        ]

        contact_id, err = create_contact(
            client, "loc1", "key", {"email": "a@b.com", "phone": "+84912345678"}
        )

        assert contact_id is None
        assert "422" in err and "country must be valid" in err

    def test_does_not_retry_unrelated_failure(self):
        client = MagicMock()
        client.post.return_value = self._resp(
            422, text='{"message":["country must be valid"]}'
        )

        contact_id, err = create_contact(
            client, "loc1", "key", {"email": "a@b.com", "phone": "+84912345678"}
        )

        assert contact_id is None
        assert client.post.call_count == 1
        assert "country must be valid" in err

    def test_surfaces_exception_as_error(self):
        client = MagicMock()
        client.post.side_effect = RuntimeError("connection reset")

        contact_id, err = create_contact(client, "loc1", "key", {"email": "a@b.com"})

        assert contact_id is None
        assert "connection reset" in err

"""
Guards that "nothing to send" never logs as a failure.

A reservation with no guest email (walk-ins, OTA bookings with masked
contacts) is routine. Logging those as failures put 51 rows behind the
"Failed only" filter when barely any were real incidents, which makes the
filter worse than useless — it hides the incidents that matter.
"""
from app.services.webhook_log import is_nothing_to_send


class TestIsNothingToSend:
    def test_google_ads_missing_identifiers(self):
        assert is_nothing_to_send(
            {"success": False, "case": "skipped_no_identifiers"}
        ) is True

    def test_meta_and_tiktok_missing_user_data(self):
        assert is_nothing_to_send({"success": False, "error": "no_user_data"}) is True

    def test_real_upload_failure_is_not_a_skip(self):
        assert is_nothing_to_send(
            {
                "success": False,
                "case": "both",
                "status_code": 404,
                "error": "Conversion action ID is not valid.",
            }
        ) is False

    def test_bad_timestamp_stays_a_failure(self):
        # Nothing was uploaded and the data is wrong — that is an incident,
        # unlike a booking that simply carried no contact details.
        assert is_nothing_to_send(
            {"success": False, "error": "invalid_conversion_time"}
        ) is False
        assert is_nothing_to_send(
            {"success": False, "error": "invalid_event_time"}
        ) is False

    def test_token_failure_stays_a_failure(self):
        assert is_nothing_to_send(
            {"success": False, "case": "both", "error": "token_refresh_failed"}
        ) is False

    def test_successful_upload_is_not_a_skip(self):
        assert is_nothing_to_send({"success": True, "case": "email_only"}) is False

    def test_empty_result_is_not_a_skip(self):
        assert is_nothing_to_send({}) is False
        assert is_nothing_to_send(None) is False

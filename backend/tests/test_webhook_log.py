"""Tests for services/webhook_log.py failure classification and dedup cache."""
from unittest.mock import MagicMock, patch

import app.services.webhook_log as webhook_log
from app.services.webhook_log import _is_failure


class TestIsFailure:
    def test_success_false_is_a_failure(self):
        assert _is_failure({"success": False, "error": "boom"}) is True

    def test_success_true_is_not(self):
        assert _is_failure({"success": True, "action": "created"}) is False

    def test_skipped_is_not_a_failure(self):
        # success=None means skipped (no config / website source). Counting it
        # as a failure would make the failure filter useless.
        assert _is_failure({"success": None, "action": "skipped_no_config"}) is False

    def test_missing_service_is_not_a_failure(self):
        assert _is_failure(None) is False
        assert _is_failure({}) is False


class TestDedupCache:
    def setup_method(self):
        webhook_log._seen_cache.clear()

    def teardown_method(self):
        webhook_log._seen_cache.clear()

    def test_cached_id_skips_the_database(self):
        webhook_log.mark_seen("R-1")
        with patch.object(webhook_log, "SessionLocal") as session:
            assert webhook_log.has_seen("R-1") is True
            session.assert_not_called()

    def test_unknown_id_falls_through_to_database(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with patch.object(webhook_log, "SessionLocal", return_value=db):
            assert webhook_log.has_seen("R-2") is False
        db.close.assert_called_once()

    def test_database_hit_populates_the_cache(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = ("row",)
        with patch.object(webhook_log, "SessionLocal", return_value=db):
            assert webhook_log.has_seen("R-3") is True
        # Second call must not need the DB again
        with patch.object(webhook_log, "SessionLocal") as session:
            assert webhook_log.has_seen("R-3") is True
            session.assert_not_called()

    def test_reservation_table_hit_counts_as_seen(self):
        # No webhook_events row (its history aged out), but the reservation
        # already exists in the permanent `reservations` table — e.g. an old,
        # checked-out booking Cloudbeds resurfaced via an edit. Must not be
        # treated as a brand-new event.
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, ("row",)]
        with patch.object(webhook_log, "SessionLocal", return_value=db):
            assert webhook_log.has_seen("R-5") is True
        db.close.assert_called_once()

    def test_lookup_failure_fails_open(self):
        # Re-processing is cheap (all three platforms dedupe on event id);
        # dropping a real booking is not. A broken lookup must not skip.
        db = MagicMock()
        db.query.side_effect = RuntimeError("connection reset")
        with patch.object(webhook_log, "SessionLocal", return_value=db):
            assert webhook_log.has_seen("R-4") is False
        db.close.assert_called_once()

    def test_cache_is_bounded(self):
        webhook_log._seen_cache.update(
            str(i) for i in range(webhook_log._SEEN_CACHE_MAX)
        )
        webhook_log.mark_seen("overflow")
        assert len(webhook_log._seen_cache) < webhook_log._SEEN_CACHE_MAX


class TestRecord:
    def test_marks_has_failure_when_any_service_failed(self):
        db = MagicMock()
        with patch.object(webhook_log, "SessionLocal", return_value=db):
            webhook_log.record(
                reservation_id="R-9",
                branch="saigon",
                guest_email="a@b.com",
                source="booking.com",
                ghl={"success": True, "action": "created"},
                google_ads={"success": False, "error": "404"},
                meta={"success": None, "action": "skipped_website_source"},
            )
        event = db.add.call_args.args[0]
        assert event.has_failure is True
        assert event.reservation_id == "R-9"
        db.commit.assert_called_once()
        db.close.assert_called_once()

    def test_all_green_or_skipped_is_not_a_failure(self):
        db = MagicMock()
        with patch.object(webhook_log, "SessionLocal", return_value=db):
            webhook_log.record(
                reservation_id="R-10",
                branch="osaka",
                guest_email="a@b.com",
                source="website/booking engine",
                ghl={"success": True, "action": "updated"},
                meta={"success": None, "action": "skipped_website_source"},
            )
        assert db.add.call_args.args[0].has_failure is False

    def test_write_failure_does_not_propagate(self):
        # Losing a monitor row must never take down the fan-out that produced it.
        db = MagicMock()
        db.commit.side_effect = RuntimeError("db down")
        with patch.object(webhook_log, "SessionLocal", return_value=db):
            webhook_log.record("R-11", "saigon", "a@b.com", "agoda")
        db.rollback.assert_called_once()
        db.close.assert_called_once()

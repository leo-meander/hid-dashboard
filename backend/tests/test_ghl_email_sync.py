"""Tests for services/ghl_email_sync.py — focuses on attribution logic."""
from datetime import date
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

import app.services.ghl_email_sync as mod
from app.services.ghl_email_sync import (
    _compute_attribution,
    _parse_workflow_created,
    _resolve_vnd_rate,
)


def _res(status="checked_out", rate_plan=None, room_type=None, native=0.0, vnd=0.0, nights=1):
    return SimpleNamespace(
        status=status,
        rate_plan_name=rate_plan,
        room_type=room_type,
        grand_total_native=native,
        grand_total_vnd=vnd,
        nights=nights,
    )


class TestParseWorkflowCreated:
    def test_iso_with_z(self):
        wf = {"dateAdded": "2026-03-25T20:05:00.000Z"}
        assert _parse_workflow_created(wf) == date(2026, 3, 25)

    def test_iso_no_z(self):
        wf = {"createdAt": "2026-04-20T10:27:00+00:00"}
        assert _parse_workflow_created(wf) == date(2026, 4, 20)

    def test_missing(self):
        assert _parse_workflow_created({}) is None

    def test_malformed(self):
        assert _parse_workflow_created({"dateAdded": "not a date"}) is None


class TestResolveVndRate:
    def test_vnd_returns_one(self):
        assert _resolve_vnd_rate("VND") == 1.0

    def test_falls_back_to_hardcoded(self):
        # No cache, no API key → uses hardcoded fallback
        rate = _resolve_vnd_rate("TWD")
        assert rate == 830.0

    def test_unknown_currency(self):
        assert _resolve_vnd_rate("XYZ") is None

    def test_none_currency(self):
        assert _resolve_vnd_rate(None) is None


class TestComputeAttribution:
    def _setup_query(self, monkeypatch, reservations):
        """Mock db.query(Reservation).filter(...).filter(...).all() chain."""
        chain = MagicMock()
        chain.filter.return_value = chain
        chain.all.return_value = reservations
        db = MagicMock()
        db.query.return_value = chain
        return db

    def test_no_branch_id_returns_empty(self):
        db = MagicMock()
        result = _compute_attribution(db, "April 2026", date(2026, 3, 25), None, "TWD")
        assert result["attributed_bookings"] == 0
        assert result["attributed_revenue_native"] == 0.0
        assert result["attributed_rate_plan"] == "CRM_April 2026 Events"

    def test_aggregates_revenue_and_excludes_canceled(self):
        reservations = [
            _res(status="checked_out", native=10000.0, vnd=8_300_000.0, nights=2),
            _res(status="confirmed", native=15000.0, vnd=12_450_000.0, nights=3),
            _res(status="canceled", native=5000.0, vnd=4_150_000.0, nights=1),
        ]
        db = self._setup_query(None, reservations)
        result = _compute_attribution(db, "April 2026", date(2026, 3, 25), "branch-uuid", "TWD")

        assert result["attributed_bookings"] == 2
        assert result["attributed_canceled"] == 1
        assert result["attributed_nights"] == 5
        assert result["attributed_revenue_native"] == 25000.0
        # Pre-converted vnd is summed when present
        assert result["attributed_revenue_vnd"] == 20_750_000.0
        assert result["attributed_currency"] == "TWD"
        assert result["attributed_rate_plan"] == "CRM_April 2026 Events"

    def test_falls_back_to_fx_when_vnd_zero(self):
        reservations = [
            _res(status="checked_out", native=10000.0, vnd=0.0, nights=2),
        ]
        db = self._setup_query(None, reservations)
        result = _compute_attribution(db, "April 2026", date(2026, 3, 25), "branch-uuid", "TWD")

        # Falls back to hardcoded TWD→VND=830.0
        assert result["attributed_revenue_vnd"] == 8_300_000.0

    def test_canceled_status_case_insensitive(self):
        reservations = [
            _res(status="Canceled", native=10000.0, nights=2),
            _res(status="CANCELED", native=20000.0, nights=3),
        ]
        db = self._setup_query(None, reservations)
        result = _compute_attribution(db, "April 2026", date(2026, 3, 25), "branch-uuid", "TWD")

        assert result["attributed_bookings"] == 0
        assert result["attributed_canceled"] == 2
        assert result["attributed_revenue_native"] == 0.0

    def test_pattern_format(self):
        db = self._setup_query(None, [])
        result = _compute_attribution(db, "Event May 2026", None, "branch-uuid", "JPY")
        assert result["attributed_rate_plan"] == "CRM_Event May 2026 Events"

"""Tests for services/rate_plan_quota_engine.py — bucket logic + counting +
email dedupe. The Cloudbeds sync side is mocked because it hits the network."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

import app.services.rate_plan_quota_engine as mod
from app.services.rate_plan_quota_engine import _current_bucket


# ── Bucket math ──────────────────────────────────────────────────────────────

class TestCurrentBucket:
    def test_below_threshold_returns_zero(self):
        assert _current_bucket(consumed_pct=80, threshold_pct=90) == 0

    def test_at_90_with_default_threshold(self):
        assert _current_bucket(consumed_pct=90.0, threshold_pct=90) == 90

    def test_at_95(self):
        assert _current_bucket(consumed_pct=95.5, threshold_pct=90) == 95

    def test_at_100(self):
        assert _current_bucket(consumed_pct=100.0, threshold_pct=90) == 100

    def test_over_100(self):
        assert _current_bucket(consumed_pct=140.0, threshold_pct=90) == 100

    def test_threshold_95_skips_90_bucket(self):
        # User-set threshold 95 → 90 bucket never fires.
        assert _current_bucket(consumed_pct=92.0, threshold_pct=95) == 0
        assert _current_bucket(consumed_pct=95.0, threshold_pct=95) == 95
        assert _current_bucket(consumed_pct=100.0, threshold_pct=95) == 100


# ── Branch scoping ───────────────────────────────────────────────────────────

class TestScopeBranches:
    def _branch(self, name):
        b = SimpleNamespace(id=uuid4(), name=name, is_active=True,
                            cloudbeds_property_id="p", currency="VND")
        return b

    def test_all_excl_oani(self):
        branches = [self._branch(n) for n in ("Saigon", "Taipei", "Oani", "Osaka")]
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = branches
        quota = SimpleNamespace(branch_scope="all_excl_oani", branch_ids=None)
        result = mod._scope_branches(db, quota)
        names = sorted(b.name for b in result)
        assert names == ["Osaka", "Saigon", "Taipei"]
        assert "Oani" not in names

    def test_specific_filters_by_ids(self):
        b1 = self._branch("Saigon")
        b2 = self._branch("Taipei")
        db = MagicMock()
        # filter() chain returns just b1
        chain = MagicMock()
        chain.all.return_value = [b1]
        db.query.return_value.filter_by.return_value.filter.return_value = chain
        quota = SimpleNamespace(branch_scope="specific", branch_ids=[str(b1.id)])
        result = mod._scope_branches(db, quota)
        assert result == [b1]


# ── Email dedupe (the part that actually keeps the inbox sane) ───────────────

class TestEvaluateAlertDedupe:
    """Verify that holding inside a bucket doesn't re-email, but crossing
    into a higher bucket does. This is the load-bearing behavior — it's
    what stops the 30-min cron from spamming the team."""

    def _make_quota(self, *, limit=100, threshold=90, last_bucket=0,
                    active_count=0):
        status = SimpleNamespace(
            active_count=active_count,
            canceled_count=0,
            consumed_pct=0,
            by_branch=None,
            last_alert_bucket=last_bucket,
            last_alerted_at=None,
            evaluated_at=None,
        )
        quota = SimpleNamespace(
            id=uuid4(),
            rate_plan_name="CRM_June 2026 Events",
            display_name=None,
            limit_count=limit,
            alert_threshold_pct=threshold,
            branch_scope="all_excl_oani",
            branch_ids=None,
            notify_email=True,
            is_active=True,
            status=status,
        )
        return quota

    def _run(self, quota, active_count, *, send_ok=True):
        """Run evaluate_quotas with all collaborators stubbed.

        Returns (sent_email_called, last_alert_bucket_after).
        """
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [quota]
        session_factory = MagicMock(return_value=db)

        with patch.object(mod, "_scope_branches", return_value=[]) as _sb, \
             patch.object(mod, "_count_for_quota",
                          return_value=(active_count, 0, [])) as _cnt, \
             patch.object(mod, "_refresh_branches_from_cloudbeds") as _sync, \
             patch.object(mod, "_send_alert_email", return_value=send_ok) as _email:
            mod.evaluate_quotas(session_factory, refresh=False)

        return _email.called, quota.status.last_alert_bucket

    def test_below_threshold_no_email(self):
        quota = self._make_quota(limit=100, threshold=90, last_bucket=0)
        called, bucket_after = self._run(quota, active_count=80)
        assert called is False
        assert bucket_after == 0

    def test_first_time_at_90_emails(self):
        quota = self._make_quota(limit=100, threshold=90, last_bucket=0)
        called, bucket_after = self._run(quota, active_count=92)
        assert called is True
        assert bucket_after == 90

    def test_holding_at_90_does_not_re_email(self):
        # We already emailed at 90 (last_bucket=90). Count is still in 90
        # bucket → must NOT re-email.
        quota = self._make_quota(limit=100, threshold=90, last_bucket=90)
        called, bucket_after = self._run(quota, active_count=93)
        assert called is False
        assert bucket_after == 90

    def test_crossing_from_90_to_95_emails(self):
        quota = self._make_quota(limit=100, threshold=90, last_bucket=90)
        called, bucket_after = self._run(quota, active_count=96)
        assert called is True
        assert bucket_after == 95

    def test_crossing_to_100_emails(self):
        quota = self._make_quota(limit=100, threshold=90, last_bucket=95)
        called, bucket_after = self._run(quota, active_count=100)
        assert called is True
        assert bucket_after == 100

    def test_falling_back_below_threshold_resets_bucket(self):
        # User had alerted at 95, then a wave of cancellations dropped count
        # back to 70%. Bucket should reset so a future climb re-fires.
        quota = self._make_quota(limit=100, threshold=90, last_bucket=95)
        called, bucket_after = self._run(quota, active_count=70)
        assert called is False
        assert bucket_after == 0

    def test_email_failure_does_not_advance_bucket(self):
        # If SendGrid is down, we want to retry on next tick — don't
        # advance the bucket so we re-attempt next cron.
        quota = self._make_quota(limit=100, threshold=90, last_bucket=0)
        called, bucket_after = self._run(quota, active_count=92, send_ok=False)
        assert called is True
        assert bucket_after == 0

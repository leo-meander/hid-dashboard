"""Tests for services/rate_plan_quota_engine.py — bucket logic + per-branch
counting + email dedupe. The Cloudbeds sync side is mocked because it hits
the network."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

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
    def _branch(self, name, bid=None):
        return SimpleNamespace(
            id=bid or uuid4(), name=name, is_active=True,
            cloudbeds_property_id="p", currency="VND",
        )

    def test_scope_returns_only_branches_in_limits(self):
        b1 = self._branch("Saigon")
        b2 = self._branch("Taipei")
        db = MagicMock()
        chain = MagicMock()
        chain.all.return_value = [b1, b2]
        db.query.return_value.filter.return_value = chain
        quota = SimpleNamespace(
            branch_limits={str(b1.id): 30, str(b2.id): 50},
        )
        result = mod._scope_branches(db, quota)
        assert result == [b1, b2]

    def test_empty_limits_returns_empty(self):
        db = MagicMock()
        quota = SimpleNamespace(branch_limits={})
        assert mod._scope_branches(db, quota) == []
        # When there's nothing to scope we shouldn't even hit the DB.
        db.query.assert_not_called()


# ── Email dedupe (per-branch — the part that keeps the inbox sane) ──────────

class TestEvaluateAlertDedupe:
    """Verify per-branch bucket tracking: a branch crossing a threshold emails
    just for that branch, and holding inside the same bucket does NOT re-email.
    Other branches' bucket histories are independent.
    """

    def _branch(self, name):
        return SimpleNamespace(
            id=uuid4(), name=name, is_active=True,
            cloudbeds_property_id="p", currency="VND",
        )

    def _make_quota(self, branches, *, caps, threshold=90, last_buckets=None):
        """branches: list of SimpleNamespace branches; caps: dict by name."""
        limits = {str(b.id): caps[b.name] for b in branches}
        status = SimpleNamespace(
            active_count=0,
            canceled_count=0,
            consumed_pct=0,
            by_branch=None,
            last_alert_buckets={
                str(b.id): (last_buckets or {}).get(b.name, 0) for b in branches
            },
            last_alerted_at=None,
            evaluated_at=None,
        )
        quota = SimpleNamespace(
            id=uuid4(),
            rate_plan_name="CRM_June 2026 Events",
            display_name=None,
            branch_limits=limits,
            alert_threshold_pct=threshold,
            notify_email=True,
            is_active=True,
            status=status,
        )
        return quota

    def _run(self, quota, branches, breakdown_by_name, *, send_ok=True):
        """Run evaluate_quotas with mocked counter + sync + email.

        breakdown_by_name maps branch name → (active, canceled). _count_for_quota
        is patched to assemble a breakdown using the quota's own limits so the
        engine sees realistic per-branch consumed_pct values.
        """
        def fake_count(_db, q, _branches):
            limits = q.branch_limits
            rows = []
            total_a = total_c = 0
            for b in branches:
                active, canceled = breakdown_by_name.get(b.name, (0, 0))
                cap = int(limits[str(b.id)])
                pct = (active / cap * 100) if cap > 0 else 0
                rows.append({
                    "branch_id": str(b.id),
                    "branch_name": b.name,
                    "active": active,
                    "canceled": canceled,
                    "limit": cap,
                    "consumed_pct": round(pct, 2),
                })
                total_a += active
                total_c += canceled
            return total_a, total_c, rows

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [quota]
        session_factory = MagicMock(return_value=db)

        with patch.object(mod, "_scope_branches", return_value=branches), \
             patch.object(mod, "_count_for_quota", side_effect=fake_count), \
             patch.object(mod, "_refresh_branches_from_cloudbeds"), \
             patch.object(mod, "_send_alert_email", return_value=send_ok) as _email:
            mod.evaluate_quotas(session_factory, refresh=False)

        return _email

    def test_below_threshold_no_email(self):
        b = [self._branch("Saigon"), self._branch("Taipei")]
        q = self._make_quota(b, caps={"Saigon": 100, "Taipei": 100})
        email = self._run(q, b, {"Saigon": (50, 0), "Taipei": (60, 0)})
        assert email.called is False
        assert all(v == 0 for v in q.status.last_alert_buckets.values())

    def test_only_one_branch_crossing_emails_just_that_branch(self):
        # Saigon at 92% (crosses 90), Taipei at 50% (well below).
        b = [self._branch("Saigon"), self._branch("Taipei")]
        q = self._make_quota(b, caps={"Saigon": 100, "Taipei": 100})
        email = self._run(q, b, {"Saigon": (92, 0), "Taipei": (50, 0)})
        assert email.called is True
        # The crossed list passed to email should contain only Saigon.
        crossed = email.call_args.args[1]
        assert [c["branch_name"] for c in crossed] == ["Saigon"]
        assert crossed[0]["new_bucket"] == 90
        # Buckets persisted: Saigon=90, Taipei=0 (independent histories).
        saigon_id = str(b[0].id)
        taipei_id = str(b[1].id)
        assert q.status.last_alert_buckets[saigon_id] == 90
        assert q.status.last_alert_buckets[taipei_id] == 0

    def test_holding_at_90_does_not_re_email(self):
        # Already alerted Saigon at 90 — count still in 90 bucket → no re-email.
        b = [self._branch("Saigon")]
        q = self._make_quota(b, caps={"Saigon": 100}, last_buckets={"Saigon": 90})
        email = self._run(q, b, {"Saigon": (93, 0)})
        assert email.called is False
        assert q.status.last_alert_buckets[str(b[0].id)] == 90

    def test_crossing_to_higher_bucket_re_emails(self):
        # Saigon was at 90, climbs into 95 bucket — re-email and bump.
        b = [self._branch("Saigon")]
        q = self._make_quota(b, caps={"Saigon": 100}, last_buckets={"Saigon": 90})
        email = self._run(q, b, {"Saigon": (96, 0)})
        assert email.called is True
        crossed = email.call_args.args[1]
        assert crossed[0]["new_bucket"] == 95
        assert q.status.last_alert_buckets[str(b[0].id)] == 95

    def test_two_branches_crossing_in_same_tick_one_email(self):
        # Both branches cross — one digest email per quota with both rows.
        b = [self._branch("Saigon"), self._branch("Taipei")]
        q = self._make_quota(b, caps={"Saigon": 100, "Taipei": 100})
        email = self._run(q, b, {"Saigon": (92, 0), "Taipei": (100, 0)})
        assert email.call_count == 1
        crossed = email.call_args.args[1]
        names = sorted(c["branch_name"] for c in crossed)
        assert names == ["Saigon", "Taipei"]
        buckets_by_name = {c["branch_name"]: c["new_bucket"] for c in crossed}
        assert buckets_by_name == {"Saigon": 90, "Taipei": 100}

    def test_falling_back_below_threshold_resets_only_that_branch(self):
        # Saigon was at 95, cancellations drop it to 70 → reset its bucket.
        # Taipei untouched at 0.
        b = [self._branch("Saigon"), self._branch("Taipei")]
        q = self._make_quota(b, caps={"Saigon": 100, "Taipei": 100},
                             last_buckets={"Saigon": 95, "Taipei": 0})
        email = self._run(q, b, {"Saigon": (70, 0), "Taipei": (40, 0)})
        assert email.called is False
        assert q.status.last_alert_buckets[str(b[0].id)] == 0
        assert q.status.last_alert_buckets[str(b[1].id)] == 0

    def test_email_failure_does_not_advance_buckets(self):
        # SendGrid down → keep buckets so next cron tick re-attempts.
        b = [self._branch("Saigon")]
        q = self._make_quota(b, caps={"Saigon": 100})
        email = self._run(q, b, {"Saigon": (92, 0)}, send_ok=False)
        assert email.called is True
        assert q.status.last_alert_buckets[str(b[0].id)] == 0

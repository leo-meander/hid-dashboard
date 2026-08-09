"""The GA4 runReport call itself — request shape and response handling.

The request shape is load-bearing, not incidental: a hostName filter would
exclude the purchase events (they fire on the Cloudbeds booking-engine domain,
and hostName is event-scoped) and quietly drive the rate toward zero.
"""
import base64
import json

import pytest

from app.services import ga4_service


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the one POST runReport makes."""

    def __init__(self, sink, response):
        self._sink = sink
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None, data=None):
        self._sink.append({"url": url, "json": json, "headers": headers, "data": data})
        return self._response


def _report(rate="0.0163", total="10000", active="9800", purchases="163",
            thresholded=False):
    return {
        "rows": [{"metricValues": [
            {"value": rate}, {"value": total}, {"value": active}, {"value": purchases},
        ]}],
        "metadata": {"subjectToThresholding": thresholded},
    }


@pytest.fixture
def call(monkeypatch):
    """Run runReport against a canned response; returns (sink, run)."""
    sink = []
    monkeypatch.setattr(ga4_service, "_access_token", lambda: ("fake-token", None))

    def run(payload=None, status_code=200):
        response = _FakeResponse(status_code, payload if payload is not None else _report())
        monkeypatch.setattr(ga4_service.httpx, "Client",
                            lambda *a, **kw: _FakeClient(sink, response))
        return ga4_service.run_purchase_report("284939713", "2026-08-01", "2026-08-31")

    return sink, run


@pytest.fixture
def why(monkeypatch):
    """Same, but returns the failure reason the debug endpoint would show."""
    sink = []
    monkeypatch.setattr(ga4_service, "_access_token", lambda: ("fake-token", None))

    def run(payload=None, status_code=200):
        response = _FakeResponse(status_code, payload if payload is not None else _report())
        monkeypatch.setattr(ga4_service.httpx, "Client",
                            lambda *a, **kw: _FakeClient(sink, response))
        return ga4_service.describe_purchase_report(
            "284939713", "2026-08-01", "2026-08-31")["error"]

    return run


# ── Request shape ────────────────────────────────────────────────────────────

def test_the_window_is_sent_as_one_date_range(call):
    sink, run = call
    run()
    assert sink[0]["json"]["dateRanges"] == [
        {"startDate": "2026-08-01", "endDate": "2026-08-31"}
    ]


def test_no_dimension_filter_is_ever_sent(call):
    """A hostName filter would exclude the purchases themselves."""
    sink, run = call
    run()
    assert "dimensionFilter" not in sink[0]["json"]


def test_no_dimensions_are_requested(call):
    """The KPI is a single property-level number; GA4 computes totals
    independently of any row breakdown."""
    sink, run = call
    run()
    assert "dimensions" not in sink[0]["json"]


def test_the_supporting_metrics_travel_with_the_rate(call):
    sink, run = call
    run()
    assert [m["name"] for m in sink[0]["json"]["metrics"]] == [
        "userKeyEventRate:purchase", "totalUsers", "activeUsers", "keyEvents:purchase",
    ]


def test_the_property_is_addressed_by_id(call):
    sink, run = call
    run()
    assert sink[0]["url"].endswith("/properties/284939713:runReport")
    assert sink[0]["headers"]["Authorization"] == "Bearer fake-token"


# ── Response handling ────────────────────────────────────────────────────────

def test_the_decimal_rate_becomes_a_percentage(call):
    _sink, run = call
    assert run().rate_pct == 1.63


def test_the_supporting_counts_come_through(call):
    _sink, run = call
    reading = run()
    assert (reading.total_users, reading.active_users, reading.purchasing_users) == (
        10000.0, 9800.0, 163.0
    )


def test_thresholding_is_read_off_the_response(call):
    _sink, run = call
    assert run(_report(thresholded=True)).thresholded is True


def test_an_empty_report_is_no_reading_rather_than_zero(call):
    _sink, run = call
    assert run({"rows": [], "metadata": {"emptyReason": "NO_DATA"}}) is None


def test_a_403_is_no_reading(call):
    """The service account is not a Viewer on the property."""
    _sink, run = call
    assert run({"error": {"code": 403}}, status_code=403) is None


def test_a_truncated_metric_list_is_rejected(call):
    _sink, run = call
    assert run({"rows": [{"metricValues": [{"value": "0.0163"}]}]}) is None


def test_no_credentials_means_no_call(monkeypatch):
    sink = []
    monkeypatch.setattr(ga4_service, "_access_token", lambda: (None, "no key"))
    monkeypatch.setattr(ga4_service.httpx, "Client",
                        lambda *a, **kw: _FakeClient(sink, _FakeResponse()))
    assert ga4_service.run_purchase_report("284939713", "2026-08-01", "2026-08-31") is None
    assert sink == []


# ── Why a cell is blank ──────────────────────────────────────────────────────
#
# A blank cell has several very different causes and they are not
# distinguishable from the outside. Each one has to name itself.

def test_a_403_names_the_missing_viewer_role(why):
    reason = why({"error": {"code": 403, "message": "caller does not have permission"}},
                 status_code=403)
    assert "403" in reason and "Viewer" in reason and "284939713" in reason


def test_an_empty_report_says_so_rather_than_looking_like_a_failure(why):
    assert "no rows" in why({"rows": [], "metadata": {"emptyReason": "NO_DATA"}})
    assert "NO_DATA" in why({"rows": [], "metadata": {"emptyReason": "NO_DATA"}})


def test_other_http_errors_carry_their_status(why):
    assert "429" in why({"error": {"code": 429}}, status_code=429)


def test_a_successful_read_has_no_error(why):
    assert why() is None


@pytest.mark.parametrize("raw,expected", [
    ("",                    "is not set"),
    ("not json",            "not valid JSON"),
    ('["a"]',               "not a JSON object"),
    ('{"client_email":"x"}', "missing private_key"),
    ('{"private_key":"y"}',  "missing client_email"),
])
def test_a_bad_service_account_key_says_what_is_wrong(monkeypatch, raw, expected):
    from app.config import settings
    monkeypatch.setattr(settings, "GA4_SERVICE_ACCOUNT_JSON", raw)
    ga4_service.reset_token_cache()
    _token, reason = ga4_service._access_token()
    assert expected in reason


def test_the_json_error_describes_the_value_without_quoting_it(monkeypatch):
    """It travels to an unauthenticated endpoint — say the shape, not the bytes."""
    from app.config import settings
    secret = "-----BEGIN PRIVATE KEY-----abcdefghijklmnop"
    monkeypatch.setattr(settings, "GA4_SERVICE_ACCOUNT_JSON", secret)
    ga4_service.reset_token_cache()
    _token, reason = ga4_service._access_token()
    assert str(len(secret)) in reason        # length is useful
    assert "abcdefghij" not in reason        # contents are not disclosed


# ── Key material a PaaS env var mangled ──────────────────────────────────────

KEY = '{"client_email": "x@y.iam.gserviceaccount.com", "private_key": "k"}'


@pytest.mark.parametrize("stored,label", [
    (KEY,                                                    "plain"),
    ("﻿" + KEY,                                         "utf-8 BOM"),
    (f"'{KEY}'",                                             "single-quoted"),
    (f'"{KEY}"',                                             "double-quoted"),
    (base64.b64encode(KEY.encode()).decode(),                "base64"),
    ("  " + base64.b64encode(KEY.encode()).decode() + "  ",  "padded base64"),
])
def test_the_key_survives_common_env_var_mangling(monkeypatch, stored, label):
    from app.config import settings
    monkeypatch.setattr(settings, "GA4_SERVICE_ACCOUNT_JSON", stored)
    info, reason = ga4_service._service_account_info()
    assert reason is None, f"{label} should parse: {reason}"
    assert info["client_email"] == "x@y.iam.gserviceaccount.com"


def test_an_unsignable_key_points_at_the_signing_step(monkeypatch):
    """A missing `cryptography` surfaces here, not as a mystery blank cell."""
    from app.config import settings
    monkeypatch.setattr(settings, "GA4_SERVICE_ACCOUNT_JSON",
                        '{"client_email":"x@y.iam.gserviceaccount.com","private_key":"not-a-key"}')
    ga4_service.reset_token_cache()
    _token, reason = ga4_service._access_token()
    assert "sign the JWT assertion" in reason

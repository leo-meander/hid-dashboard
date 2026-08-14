"""Filter tests for the lead-time cohort query.

These assert the generated SQL rather than query results, because the whole
bug class this endpoint exists to fix is structural: the pre-existing tools
answered "bookings in Q4" by filtering check_in_date, so any question phrased
around the *booking* date got the wrong population entirely. A results test
would need a live DB; the column choice and the band bounds are exactly what
must not silently regress, and those are visible in the SQL.
"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.services.metrics_engine import build_lead_time_cohort_query

# create_engine does not connect; the session is only used to build queries.
_engine = create_engine("postgresql://test:test@localhost/test")
_Session = sessionmaker(bind=_engine)

Q4_FROM = date(2025, 10, 1)
Q4_TO = date(2025, 12, 31)


def _sql(**kwargs):
    db = _Session()
    try:
        q = build_lead_time_cohort_query(
            db,
            kwargs.pop("branch_id", None),
            kwargs.pop("date_from", Q4_FROM),
            kwargs.pop("date_to", Q4_TO),
            kwargs.pop("lead_time_min", 0),
            kwargs.pop("lead_time_max", None),
            kwargs.pop("source", None),
        )
        return str(q.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        ))
    finally:
        db.close()


def test_window_filters_booking_date_not_check_in_date():
    sql = _sql()
    assert "reservations.reservation_date >= '2025-10-01'" in sql
    assert "reservations.reservation_date <= '2025-12-31'" in sql
    # check_in_date may only appear inside the lead-time difference, never as
    # the window filter — that confusion is the original bug.
    assert "reservations.check_in_date >=" not in sql
    assert "reservations.check_in_date <=" not in sql


def test_lead_time_is_check_in_minus_booking_date():
    sql = _sql(lead_time_min=31)
    assert "reservations.check_in_date - reservations.reservation_date >= 31" in sql


def test_open_ended_band_emits_no_upper_bound():
    """'More than 30 days' must not silently cap. booking-pace capped at 90 and
    quietly dropped the long tail; this must not."""
    sql = _sql(lead_time_min=31, lead_time_max=None)
    assert "reservations.check_in_date - reservations.reservation_date >= 31" in sql
    assert "reservations.check_in_date - reservations.reservation_date <=" not in sql


def test_bounded_band_is_inclusive_on_both_ends():
    sql = _sql(lead_time_min=31, lead_time_max=60)
    assert "reservations.check_in_date - reservations.reservation_date >= 31" in sql
    assert "reservations.check_in_date - reservations.reservation_date <= 60" in sql


def test_cancelled_and_non_paying_sources_are_excluded():
    sql = _sql().lower()
    for status in ("cancelled", "canceled", "no_show", "no-show"):
        assert f"'{status}'" in sql
    for source in ("blogger", "kol", "house use", "special case", "maintenance"):
        assert f"'{source}'" in sql


def test_revenue_reported_in_both_native_and_vnd():
    """Monetary values are stored in both currencies; reporting only one is how
    the VND totals got mislabelled as TWD/JPY."""
    sql = _sql()
    assert "sum(reservations.grand_total_native)" in sql
    assert "sum(reservations.grand_total_vnd)" in sql


def test_source_filter_is_a_case_insensitive_substring():
    """The stored value is "Website/Booking Engine" — normalize_source only
    canonicalises OTA names, so direct sources keep their raw Cloudbeds string
    and an exact match on "website" would silently return zero rows."""
    sql = _sql(source="website").lower()
    # ILIKE, not LIKE — case-insensitivity has to survive "Website/Booking Engine".
    assert "reservations.source ilike" in sql
    assert "'website'" in sql


def test_source_filter_escapes_like_wildcards():
    """A source containing % or _ must match literally, not as a wildcard."""
    sql = _sql(source="100%_direct")
    assert "ESCAPE" in sql.upper()


def test_no_source_filter_leaves_source_unconstrained():
    sql = _sql().lower()
    assert "reservations.source ilike" not in sql


def test_grouped_per_branch_and_scopable_to_one():
    assert "GROUP BY reservations.branch_id" in _sql()
    scoped = _sql(branch_id="11111111-1111-1111-1111-111111111101")
    assert "reservations.branch_id = '11111111-1111-1111-1111-111111111101'" in scoped

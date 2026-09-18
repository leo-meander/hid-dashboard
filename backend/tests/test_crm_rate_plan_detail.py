"""CRM rate-plan drill-down — the logic behind clicking a Rate Plan Name.

Covers the parts that decide whether the drill-down agrees with the table row
it opened (row exclusion, and the WHERE-able rate plan expression) and the
demographics maths, which has to reject Cloudbeds' placeholder birthdates.
"""
from datetime import date

from app.routers.marketing_activity import (
    _age_bucket,
    _age_on,
    _crm_row_exclusion,
    _num_stats,
)
from app.services.crm_filters import (
    crm_rate_plan_label_expr,
    crm_rate_plan_value_expr,
)


def _sql(clause):
    return str(clause.compile(compile_kwargs={"literal_binds": True})).lower()


# ── Rate plan expression ─────────────────────────────────────────────────────

def test_value_expr_is_the_label_expr_without_the_label():
    """Drilling in must filter on exactly what the grouping produced."""
    assert _sql(crm_rate_plan_value_expr()) == _sql(crm_rate_plan_label_expr())


def test_value_expr_is_usable_in_a_where_clause():
    sql = _sql(crm_rate_plan_value_expr() == "MEANDER'S FRIEND")
    assert "coalesce" in sql
    assert "reservations.rate_plan_name" in sql
    assert "reservations.room_type" in sql
    # The comparison renders, i.e. this is a predicate and not just a column.
    assert "=" in sql


# ── Row exclusion — the table/drill-down reconciliation rule ─────────────────

def test_cancelled_statuses_are_excluded():
    for status in ("Cancelled", "cancelled", "no_show", "NO-SHOW", "cancelled_by_guest"):
        assert _crm_row_exclusion(status, "Website") == "cancelled"


def test_non_paying_sources_are_excluded():
    for source in ("Blogger", "house use", "Special Case", "Work Exchange"):
        assert _crm_row_exclusion("Confirmed", source) == "non_paying_source"


def test_a_paying_confirmed_row_counts():
    assert _crm_row_exclusion("Confirmed", "Website") is None
    assert _crm_row_exclusion(None, None) is None


def test_cancelled_wins_over_source():
    """A cancelled blogger stay is reported as cancelled, counted once."""
    assert _crm_row_exclusion("Cancelled", "Blogger") == "cancelled"


# ── Age ──────────────────────────────────────────────────────────────────────

def test_age_is_the_year_difference_at_check_in():
    """Only the birth year is stored, so age is a year subtraction.

    Exact dates of birth were dropped in migration 067: every consumer bucketed
    them into an age band anyway, and a stored birthdate identifies a person in
    a way a year does not.
    """
    assert _age_on(1990, date(2026, 6, 1)) == 36
    assert _age_on(2001, date(2026, 6, 1)) == 25


def test_age_reads_up_to_a_year_high_and_that_is_accepted():
    """A guest whose birthday has not arrived yet reads one year older.

    Worth pinning rather than hiding: it is the whole cost of storing the year
    instead of the date. Someone born in December 1990 is 35 on a June 2026
    check-in, and this returns 36.
    """
    assert _age_on(1990, date(2026, 6, 1)) == 36


def test_placeholder_and_missing_birth_years_are_not_ages():
    assert _age_on(None, date(2026, 6, 1)) is None
    assert _age_on(1990, None) is None
    # Cloudbeds placeholder year — 126 years old is not a guest.
    assert _age_on(1900, date(2026, 6, 1)) is None
    # A birth year after the stay is data entry noise, not a newborn.
    assert _age_on(2027, date(2026, 6, 1)) is None


def test_age_buckets_at_their_boundaries():
    assert _age_bucket(24) == "<25"
    assert _age_bucket(25) == "25-34"
    assert _age_bucket(34) == "25-34"
    assert _age_bucket(35) == "35-44"
    assert _age_bucket(54) == "45-54"
    assert _age_bucket(55) == "55+"
    assert _age_bucket(None) is None


# ── Stats ────────────────────────────────────────────────────────────────────

def test_num_stats_median_for_odd_and_even_counts():
    assert _num_stats([3, 1, 2])["median"] == 2
    assert _num_stats([4, 1, 2, 3])["median"] == 2.5


def test_num_stats_on_empty_input_is_zeroed_not_an_error():
    assert _num_stats([]) == {"count": 0, "avg": 0, "median": 0, "min": 0, "max": 0}


def test_num_stats_reports_the_spread():
    s = _num_stats([1, 5, 3])
    assert (s["count"], s["min"], s["max"], s["avg"]) == (3, 1, 5, 3.0)

"""
Guards for what actually bounds the reservation poller.

Cloudbeds does not honour `dateCreatedFrom`/`dateCreatedTo` — verified against
production on 2026-09-11, where a 1-minute window and a 3-hour window returned
byte-identical pages for all five properties. So the window is ours to enforce,
and two things have to hold for that to be safe:

  - the window is expressed in the property's LOCAL time, because Cloudbeds'
    dateCreated is. Built from UTC, Osaka's window sat 9 hours off.
  - a full page means there is more behind it. Reading page 1 and stopping lost
    a burst of more than PAGE_SIZE reservations outright instead of delaying it.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.routers import webhooks

PAGE = webhooks.PAGE_SIZE
NOW = datetime(2026, 9, 11, 5, 0, 0, tzinfo=timezone.utc)


def _res(rid, created):
    return {"reservationID": rid, "dateCreated": created}


def _page(rows):
    """Shape of _get_reservation_list's return: (response, body, attempts)."""
    return (None, {"success": True, "data": rows}, 1)


def _run_branch(branch, pages, minutes=60):
    """Poll one branch over canned pages; returns (fanned_out_ids, requests)."""
    requests = []

    def fake_list(property_id, api_key, date_from, date_to, page_number=1):
        requests.append({"from": date_from, "to": date_to, "page": page_number})
        idx = page_number - 1
        return _page(pages[idx] if idx < len(pages) else [])

    fanned = []
    with patch.object(webhooks, "_get_reservation_list", side_effect=fake_list), \
         patch.object(webhooks.webhook_log, "has_seen", return_value=False), \
         patch.object(webhooks.webhook_log, "mark_seen"), \
         patch.object(webhooks, "_fetch_full_reservation", side_effect=lambda p, r: {"reservationID": r}), \
         patch.object(webhooks, "_fan_out", side_effect=lambda p, r, full: fanned.append(r)):
        webhooks._poll_branch(branch, "301582", "key", NOW, minutes)

    return fanned, requests


class TestWindowIsPropertyLocal:
    """Cloudbeds' dateCreated has no offset on it — it is the property's clock."""

    def test_osaka_window_is_shifted_nine_hours(self):
        # 05:00 UTC is 14:00 in Osaka. A 60-minute window therefore starts at
        # 13:00 local, not 04:00 — the UTC-built window asked for the middle of
        # last night, and would have matched nothing the day Cloudbeds honours it.
        _, requests = _run_branch("osaka", [[]])
        assert requests[0]["from"] == "2026-09-11 13:00:00"

    def test_each_branch_gets_its_own_offset(self):
        starts = {}
        for branch in ("saigon", "oani", "osaka"):
            _, requests = _run_branch(branch, [[]])
            starts[branch] = requests[0]["from"]
        # +7, +8, +9 — one hour apart, in order.
        assert starts["saigon"] == "2026-09-11 11:00:00"
        assert starts["oani"] == "2026-09-11 12:00:00"
        assert starts["osaka"] == "2026-09-11 13:00:00"

    def test_upper_bound_has_headroom_for_clock_skew(self):
        _, requests = _run_branch("osaka", [[]])
        assert requests[0]["to"] == "2026-09-11 14:05:00"


class TestAgeFilterIsEnforcedHere:
    def test_reservation_older_than_the_window_is_not_fanned_out(self):
        rows = [
            _res("fresh", "2026-09-11 13:55:00"),   # 5 min old, local
            _res("stale", "2026-09-11 11:30:00"),   # 2.5 h old — outside 60 min
        ]
        fanned, _ = _run_branch("osaka", [rows])
        assert fanned == ["fresh"]

    def test_out_of_window_rows_cost_no_dedup_lookup(self):
        # There are a lot of these behind a full page, and each has_seen miss is
        # two queries against a pool only 8 connections wide.
        rows = [_res(str(i), "2026-09-01 00:00:00") for i in range(PAGE)]

        def fake_list(property_id, api_key, date_from, date_to, page_number=1):
            return _page(rows if page_number == 1 else [])

        with patch.object(webhooks, "_get_reservation_list", side_effect=fake_list), \
             patch.object(webhooks.webhook_log, "has_seen", return_value=False) as has_seen, \
             patch.object(webhooks.webhook_log, "mark_seen"), \
             patch.object(webhooks, "_fetch_full_reservation"), \
             patch.object(webhooks, "_fan_out"):
            webhooks._poll_branch("osaka", "301582", "key", NOW, 60)

        has_seen.assert_not_called()

    def test_unreadable_date_created_is_treated_as_in_window(self):
        # Dropping a real booking is the expensive mistake; a duplicate fan-out
        # is deduped downstream on reservationID.
        fanned, _ = _run_branch("osaka", [[_res("odd", ""), _res("odd2", "not-a-date")]])
        assert fanned == ["odd", "odd2"]


class TestPagination:
    def test_a_full_page_of_in_window_rows_pulls_the_next_one(self):
        page1 = [_res("a%d" % i, "2026-09-11 13:50:00") for i in range(PAGE)]
        page2 = [_res("b1", "2026-09-11 13:40:00")]
        fanned, requests = _run_branch("osaka", [page1, page2])

        assert [r["page"] for r in requests] == [1, 2]
        assert "b1" in fanned
        assert len(fanned) == PAGE + 1

    def test_a_short_page_ends_the_walk(self):
        fanned, requests = _run_branch("osaka", [[_res("only", "2026-09-11 13:50:00")]])
        assert [r["page"] for r in requests] == [1]
        assert fanned == ["only"]

    def test_walking_stops_at_the_window_edge_not_at_the_end_of_history(self):
        page1 = [_res("a%d" % i, "2026-09-11 13:50:00") for i in range(PAGE)]
        page2 = [_res("b%d" % i, "2026-09-01 09:00:00") for i in range(PAGE)]  # all stale
        page3 = [_res("c1", "2026-08-01 09:00:00")]
        fanned, requests = _run_branch("osaka", [page1, page2, page3])

        # Page 2 is read, found to be entirely behind the window, and page 3 is
        # never asked for.
        assert [r["page"] for r in requests] == [1, 2]
        assert all(not r.startswith("b") and not r.startswith("c") for r in fanned)

    def test_page_walking_is_capped(self):
        pages = []
        for n in range(webhooks.MAX_POLL_PAGES + 5):
            pages.append([_res("p%dr%d" % (n, i), "2026-09-11 13:50:00") for i in range(PAGE)])
        _, requests = _run_branch("osaka", pages)
        assert len(requests) == webhooks.MAX_POLL_PAGES


class TestLagIsRecorded:
    """The monitor cannot show a lag the fan-out never wrote down."""

    def _fan_out_osaka(self, reservation):
        # cloudbeds_property_to_branch is a pydantic computed property, so it is
        # patched on the class rather than the settings instance.
        with patch.object(type(webhooks.settings), "cloudbeds_property_to_branch",
                          property(lambda self: {"301582": "osaka"})), \
             patch.object(webhooks.webhook_log, "record") as record, \
             patch.object(webhooks, "upsert_contact_from_reservation",
                          return_value={"action": "created", "contact_id": "c1"}), \
             patch.object(webhooks, "send_purchase_event", return_value={"success": True}), \
             patch.object(webhooks, "upload_offline_conversion",
                          return_value={"success": True, "case": "both"}), \
             patch.object(webhooks, "send_complete_payment_event", return_value={"success": True}):
            webhooks._fan_out("301582", "999", reservation)
        return record.call_args.kwargs["reservation_created_at"]

    def test_fan_out_logs_cloudbeds_date_created_in_utc(self):
        logged = self._fan_out_osaka(
            {"reservationID": "999", "dateCreated": "2026-09-11 14:00:00", "source": "jalan"}
        )
        # 14:00 in Osaka (+9) is 05:00 UTC — and no event_time_extra_offset on
        # top, which would have bent the measurement by another two hours.
        assert logged == datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc)

    def test_missing_date_created_logs_no_lag(self):
        assert self._fan_out_osaka({"reservationID": "999"}) is None


class TestLagDerivation:
    def test_lag_seconds_is_the_difference(self):
        from app.services import webhook_log

        class Row:
            created_at = NOW
            reservation_created_at = NOW - timedelta(minutes=7)

        assert webhook_log._lag_seconds(Row()) == 420

    def test_missing_creation_time_gives_no_lag(self):
        from app.services import webhook_log

        class Row:
            created_at = NOW
            reservation_created_at = None

        assert webhook_log._lag_seconds(Row()) is None

    def test_negative_lag_is_reported_not_clamped(self):
        # A negative value means the branch's tz offset is wrong. Clamping it to
        # zero turns a misconfiguration into an unusually good number.
        from app.services import webhook_log

        class Row:
            created_at = NOW
            reservation_created_at = NOW + timedelta(hours=1)

        assert webhook_log._lag_seconds(Row()) == -3600

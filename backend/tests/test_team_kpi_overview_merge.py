"""Merging of per-PIC task stats for the Task Overview tab.

get_task_overview_yearly keys results by Lark record ID, and one person can
hold several (Nora has two). merge_pic_overview collapses those into one row
per person — the step where a stat computed upstream can quietly fail to reach
the API.
"""
from app.services.lark_service import merge_pic_overview

NAMES = {"recA": "Mel", "recN1": "Nora", "recN2": "Nora", "recK": "Kin"}


def _month(**over):
    base = {
        "total_tasks": 0, "completed": 0, "on_time_count": 0, "late_count": 0,
        "on_time_filled": 0, "overdue_count": 0, "late_excused_count": 0,
        "overdue_excused_count": 0, "reason_counts": {}, "bad_duration_count": 0,
        "reopen_count": 0, "cycle_time_avg": None, "estimated_avg": None,
        "cycle_ratio": None, "completion_rate": None, "on_time_rate": None,
    }
    base.update(over)
    return base


def _merge(data, monkeypatch):
    import app.services.lark_service as L
    monkeypatch.setattr(L, "PIC_NAME_MAP", NAMES)
    return {r["name"]: r for r in merge_pic_overview(data)}


class TestPerPicTotals:
    def test_every_per_pic_total_reaches_the_result(self, monkeypatch):
        # excluded_status_count was computed upstream but never copied out of
        # the merge, so the endpoint silently omitted it.
        data = {"recA": {7: _month(), "open_workload": 3,
                         "no_deadline_count": 4, "excluded_status_count": 5}}
        row = _merge(data, monkeypatch)["Mel"]
        assert row["open_workload"] == 3
        assert row["no_deadline_count"] == 4
        assert row["excluded_status_count"] == 5

    def test_totals_sum_across_a_persons_record_ids(self, monkeypatch):
        data = {
            "recN1": {7: _month(), "open_workload": 2, "no_deadline_count": 1,
                      "excluded_status_count": 6},
            "recN2": {7: _month(), "open_workload": 3, "no_deadline_count": 4,
                      "excluded_status_count": 7},
        }
        row = _merge(data, monkeypatch)["Nora"]
        assert row["open_workload"] == 5
        assert row["no_deadline_count"] == 5
        assert row["excluded_status_count"] == 13

    def test_missing_totals_default_to_zero(self, monkeypatch):
        row = _merge({"recK": {7: _month()}}, monkeypatch)["Kin"]
        assert row["open_workload"] == 0
        assert row["excluded_status_count"] == 0

    def test_unmapped_record_ids_are_dropped(self, monkeypatch):
        data = {"recGhost": {7: _month(total_tasks=9), "open_workload": 1}}
        assert _merge(data, monkeypatch) == {}


class TestMonthCounts:
    def test_counts_sum_across_record_ids(self, monkeypatch):
        data = {
            "recN1": {7: _month(total_tasks=4, overdue_count=1, late_excused_count=2)},
            "recN2": {7: _month(total_tasks=3, overdue_count=2, late_excused_count=1)},
        }
        stats = _merge(data, monkeypatch)["Nora"]["months"][7]
        assert stats["total_tasks"] == 7
        assert stats["overdue_count"] == 3
        assert stats["late_excused_count"] == 3

    def test_months_are_kept_separate(self, monkeypatch):
        data = {"recA": {7: _month(total_tasks=2), 8: _month(total_tasks=5)}}
        months = _merge(data, monkeypatch)["Mel"]["months"]
        assert months[7]["total_tasks"] == 2
        assert months[8]["total_tasks"] == 5


class TestRates:
    def test_rates_are_recomputed_not_summed(self, monkeypatch):
        # Summing a 100% and a 50% rate used to yield 150%.
        data = {
            "recN1": {7: _month(total_tasks=4, completed=4, on_time_count=4,
                                on_time_filled=4, on_time_rate=100.0,
                                completion_rate=100.0)},
            "recN2": {7: _month(total_tasks=4, completed=2, on_time_count=1,
                                on_time_filled=2, on_time_rate=50.0,
                                completion_rate=50.0)},
        }
        stats = _merge(data, monkeypatch)["Nora"]["months"][7]
        assert stats["on_time_rate"] == round(5 / 6 * 100, 1)
        assert stats["completion_rate"] == 75.0

    def test_no_scored_tasks_leaves_the_rate_undefined(self, monkeypatch):
        data = {"recK": {7: _month(total_tasks=3, on_time_filled=0)}}
        stats = _merge(data, monkeypatch)["Kin"]["months"][7]
        assert stats["on_time_rate"] is None
        assert stats["completion_rate"] == 0.0


class TestReasonCounts:
    def test_per_reason_tallies_merge_instead_of_colliding(self, monkeypatch):
        data = {
            "recN1": {7: _month(reason_counts={"my own delay": 2,
                                               "waiting for approval": 1})},
            "recN2": {7: _month(reason_counts={"my own delay": 3})},
        }
        stats = _merge(data, monkeypatch)["Nora"]["months"][7]
        assert stats["reason_counts"] == {"my own delay": 5, "waiting for approval": 1}

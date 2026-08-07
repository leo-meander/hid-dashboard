"""Every Task Overview figure is scoped by Deadline month.

A task due in August must never be counted on a July card — not in
total_tasks, not in open_count, and not in the drilldown list behind either
number. This is the shape that made "7 tasks · 2 open" read wrong on a July
card when the two open tasks were due in August.
"""
import pytest

import app.services.lark_service as L

NAMES = {"recM": "Mason"}


def _rec(rid, deadline, status, on_time=None):
    return {
        "_record_id": rid,
        "Task": f"task-{rid}",
        "PIC": {"link_record_ids": ["recM"]},
        "Status": status,
        "Deadline": deadline,
        "Date Created": "2026-07-01",
        "On-time vs Original": on_time,
    }


# 5 completed on-time in July, 2 still open with an August deadline.
RECORDS = [
    _rec("j1", "2026-07-05", "Completed", "On-time"),
    _rec("j2", "2026-07-09", "Completed", "On-time"),
    _rec("j3", "2026-07-14", "Completed", "On-time"),
    _rec("j4", "2026-07-21", "Completed", "On-time"),
    _rec("j5", "2026-07-28", "Completed", "On-time"),
    _rec("a1", "2026-08-12", "In Progress"),
    _rec("a2", "2026-08-20", "In Progress"),
]


@pytest.fixture(autouse=True)
def _lark(monkeypatch):
    monkeypatch.setattr(L, "PIC_NAME_MAP", NAMES)
    monkeypatch.setattr(L, "_get_cached_records", lambda: RECORDS)
    monkeypatch.setattr(L, "_today_ict", lambda: L.date(2026, 8, 7))
    L._task_overview_cache.clear()
    yield
    L._task_overview_cache.clear()


def test_august_tasks_stay_out_of_july():
    months = L.get_task_overview_yearly(2026)["recM"]
    assert months[7]["total_tasks"] == 5
    assert months[7]["open_count"] == 0
    assert months[8]["total_tasks"] == 2
    assert months[8]["open_count"] == 2


def test_open_workload_counts_the_year_once():
    """The per-PIC total is year-wide on purpose — the cards use open_count."""
    assert L.get_task_overview_yearly(2026)["recM"]["open_workload"] == 2


@pytest.mark.parametrize("category", ["total", "open"])
def test_drilldown_follows_the_deadline_month(category):
    july = L.get_task_detail("Mason", 2026, 7, category)
    august = L.get_task_detail("Mason", 2026, 8, category)
    assert all(t["deadline"].startswith("2026-07") for t in july)
    assert all(t["deadline"].startswith("2026-08") for t in august)


def test_drilldown_counts_match_the_card():
    assert len(L.get_task_detail("Mason", 2026, 7, "total")) == 5
    assert L.get_task_detail("Mason", 2026, 7, "open") == []
    assert len(L.get_task_detail("Mason", 2026, 8, "open")) == 2


def test_no_month_covers_the_year_from_july():
    """Month = All on the cards: the whole tracked window, oldest deadline first."""
    all_tasks = L.get_task_detail("Mason", 2026, None, "total")
    assert len(all_tasks) == 7
    assert [t["deadline"] for t in all_tasks] == sorted(t["deadline"] for t in all_tasks)
    assert len(L.get_task_detail("Mason", 2026, None, "open")) == 2


def test_pre_july_deadlines_are_excluded_everywhere():
    june = _rec("m1", "2026-06-15", "In Progress")
    RECORDS.append(june)
    try:
        L._task_overview_cache.clear()
        months = L.get_task_overview_yearly(2026)["recM"]
        assert 6 not in months
        assert len(L.get_task_detail("Mason", 2026, None, "total")) == 7
    finally:
        RECORDS.remove(june)

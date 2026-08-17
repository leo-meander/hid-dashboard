"""Folding operator corrections into the Bi-Weekly report's flag lines.

The important case here is the deploy window. Zeabur does not run Alembic on
deploy, so between this code landing and `POST /api/sync/run-migrations` being
called, `biweekly_flag_overrides` does not exist and every report request
queries it. If that is not contained, the whole report page 500s — a feature
for correcting a sentence would have taken out the page it lives on.
"""
import pytest

from app.routers.biweekly_report import _apply_flag_overrides
from app.services.biweekly_period import period_for


P = period_for(2026, 8, 1)
BRANCH = "11111111-1111-1111-1111-111111111111"


def _payload(highlights=None, watchouts=None, actions=None):
    return [{
        "branch_id": BRANCH,
        "branch_name": "MEANDER Saigon",
        "highlights": highlights if highlights is not None else [
            {"key": "flag.revenue", "text": "Room revenue +22%."},
            {"key": "flag.markets", "text": "Taiwan growing fast — up to +111%."},
        ],
        "watchouts": watchouts if watchouts is not None else [
            {"key": "flag.occ", "text": "Occupancy -4.2%."},
        ],
        "actions": actions if actions is not None else [
            {"key": "act.kol_posts", "title": "No KOL posts", "when": "Next period",
             "body": "Chase the pipeline."},
        ],
    }]


class _Override:
    def __init__(self, flag_key, body=None, is_hidden=False, branch_id=BRANCH):
        self.flag_key = flag_key
        self.body = body
        self.is_hidden = is_hidden
        self.branch_id = branch_id


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Just enough Session for `_apply_flag_overrides`."""

    def __init__(self, rows=(), raises=False):
        self._rows = list(rows)
        self._raises = raises
        self.rolled_back = False

    def query(self, *a, **k):
        if self._raises:
            raise RuntimeError('relation "biweekly_flag_overrides" does not exist')
        return _FakeQuery(self._rows)

    def rollback(self):
        self.rolled_back = True


class TestMissingTableIsContained:
    def test_a_failing_query_serves_the_generated_lines_untouched(self):
        payload = _payload()
        out = _apply_flag_overrides(_FakeSession(raises=True), P, payload)
        assert out == payload

    def test_and_rolls_the_session_back(self):
        """A failed statement poisons the transaction; without a rollback every
        later query in the same request fails too, which would take out the
        report by a longer route."""
        db = _FakeSession(raises=True)
        _apply_flag_overrides(db, P, _payload())
        assert db.rolled_back is True


class TestFolding:
    def test_replacing_a_line_marks_it_edited_and_keeps_the_rest(self):
        db = _FakeSession([_Override("flag.markets", body="Taiwan up on 5 bookings — noise.")])
        out = _apply_flag_overrides(db, P, _payload())[0]
        assert out["highlights"][0] == {"key": "flag.revenue", "text": "Room revenue +22%."}
        assert out["highlights"][1] == {
            "key": "flag.markets",
            "text": "Taiwan up on 5 bookings — noise.",
            "edited": True,
        }

    def test_hiding_a_line_drops_it(self):
        db = _FakeSession([_Override("flag.occ", is_hidden=True)])
        out = _apply_flag_overrides(db, P, _payload())[0]
        assert out["watchouts"] == []

    def test_an_action_override_lands_in_text_not_body(self):
        """The renderer reassembles title + when-pill + body for a generated
        action. An operator writes one sentence, so it has to arrive somewhere
        the renderer will use whole."""
        db = _FakeSession([_Override("act.kol_posts", body="Skipping KOL on purpose.")])
        out = _apply_flag_overrides(db, P, _payload())[0]
        act = out["actions"][0]
        assert act["text"] == "Skipping KOL on purpose."
        assert act["edited"] is True
        assert act["body"] == "Chase the pipeline."   # original left intact

    def test_an_override_for_a_rule_that_did_not_fire_changes_nothing(self):
        db = _FakeSession([_Override("flag.direct", body="Never rendered.")])
        payload = _payload()
        assert _apply_flag_overrides(db, P, payload) == payload

    def test_another_branchs_override_does_not_leak_across(self):
        db = _FakeSession([_Override(
            "flag.revenue", body="Someone else's branch.",
            branch_id="22222222-2222-2222-2222-222222222222")])
        payload = _payload()
        assert _apply_flag_overrides(db, P, payload) == payload

    def test_a_pre_key_cached_payload_passes_through(self):
        """Periods cached before flags carried keys are lists of plain strings —
        nothing to match an override against, and nothing to crash on."""
        db = _FakeSession([_Override("flag.revenue", body="x")])
        payload = _payload(highlights=["Legacy string."], watchouts=[], actions=[])
        out = _apply_flag_overrides(db, P, payload)[0]
        assert out["highlights"] == ["Legacy string."]

    def test_no_overrides_returns_the_same_object(self):
        payload = _payload()
        assert _apply_flag_overrides(_FakeSession([]), P, payload) is payload

    @pytest.mark.parametrize("payload", [[], None])
    def test_an_empty_payload_is_handled(self, payload):
        assert _apply_flag_overrides(_FakeSession(raises=True), P, payload) == payload

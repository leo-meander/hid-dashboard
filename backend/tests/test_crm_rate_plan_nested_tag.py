"""The CRM rate plan tag must survive a nested bracket.

Cloudbeds packs the plan into room_type, and the plan names carry their own
parenthesised qualifier, so the group is nested:

    "8 Beds Mixed Dorm Shared Bathroom (Extension Promotion (>2 night))"
                                        ^--------- rate plan ---------^

The old pattern forbade brackets in the body so it could find a closer, and
therefore stopped at the INNER one, labelling the row
"Extension Promotion (>2 night" — a name missing its closing bracket that reads
like real data.

Caveat on these tests: the extraction runs in PostgreSQL, and `re` is used here
as a stand-in for its POSIX engine. The two agree on this pattern (greedy
quantifiers, and no longer match is available at the leftmost open bracket), but
only a real query proves the production behaviour.
"""
import importlib.util
import re
import sys
from pathlib import Path

from app.services.crm_filters import crm_rate_plan_value_expr

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "066_rate_plan_campaign_nested_tag.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m066", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["m066"] = mod
    spec.loader.exec_module(mod)
    return mod


def _embedded_pattern() -> str:
    """The regex as crm_filters spells it inside the Postgres E'' literal."""
    sql = str(crm_rate_plan_value_expr().compile(compile_kwargs={"literal_binds": True}))
    return re.search(r"substring\(reservations\.room_type from E'(.+?)'\)", sql).group(1)


def _as_regex(embedded: str) -> re.Pattern:
    """What the regex engine receives — an E-string turns each \\\\ into one \\."""
    return re.compile(embedded.replace("\\\\", "\\"))


# ── The re-key migration and the live pattern must not drift ────────────────

def test_migration_066_froze_the_pattern_crm_filters_still_uses():
    """rate_plan_campaigns rows were re-keyed onto exactly this extraction.

    Changing the pattern again re-labels CRM rows and strands every hand-typed
    campaign name on the old key, so it needs its own re-key migration. This
    assertion is how that gets noticed.
    """
    assert _embedded_pattern() == _load_migration()._NEW


def test_migration_patterns_survive_the_e_string_unescape():
    """A lone backslash would be eaten, turning \\( into a capture group."""
    m = _load_migration()
    assert _as_regex(m._OLD).pattern == r"\(([^)]+)\)"
    assert _as_regex(m._NEW).pattern == r"\(((?:[^()]|\([^()]*\))+)\)"


# ── What the pattern extracts ───────────────────────────────────────────────

def _tag(room_type):
    m = _as_regex(_embedded_pattern()).search(room_type)
    return m.group(1) if m else None


def test_nested_qualifier_keeps_its_closing_bracket():
    assert _tag("8 Beds Mixed Dorm Shared Bathroom (Extension Promotion (>2 night))") \
        == "Extension Promotion (>2 night)"
    assert _tag("8 Beds Mixed Dorm Shared Bathroom (Extension Promotion (1 night))") \
        == "Extension Promotion (1 night)"


def test_every_non_nested_shape_is_unchanged():
    """The fix must move nested rows only — nothing else may be re-labelled."""
    old = _as_regex(_load_migration()._OLD)
    unchanged = [
        "Standard Twin (KOL_whatweieats)",
        "Female Dorm* (CRM_May 2026 Event)",
        "Deluxe (WELCOME BACK) extra text after the group",
        # Multi-room room_type: the first group still wins, as before.
        "Room A (PLAN1) Room B (PLAN2)",
    ]
    for room_type in unchanged:
        before = old.search(room_type)
        assert _tag(room_type) == (before.group(1) if before else None), room_type


def test_a_room_type_with_no_usable_group_yields_nothing():
    # No tag, an unclosed group, and an empty one all fall through to the
    # coalesce's next branch rather than producing a junk label.
    assert _tag("No brackets at all") is None
    assert _tag("Unclosed (PLAN") is None
    assert _tag("Empty ()") is None

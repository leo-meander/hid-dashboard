"""re-key rate_plan_campaigns onto the un-truncated rate plan tag

Revision ID: 066
Revises: 065
Create Date: 2026-09-15

The CRM rate plan label is extracted from room_type at query time. Its old
pattern forbade brackets inside the group so it could locate a closer, which
made it stop at the INNER bracket of a nested tag:

    "8 Beds Mixed Dorm Shared Bathroom (Extension Promotion (>2 night))"
      old -> "Extension Promotion (>2 night"      (no closing bracket)
      new -> "Extension Promotion (>2 night)"

rate_plan_campaigns is keyed by that label, so every hand-typed campaign name
sitting on a truncated key would silently stop attaching to its row the moment
the extraction is fixed — the table would just look unlabelled again.

This re-keys those rows. The mapping is not guessed by string surgery: it is
read out of `reservations` by running the old and the new pattern over the same
room_type and keeping the pairs that differ, so a key is only rewritten to a
label the new extraction actually produces.

Rows whose new label is already taken are deleted rather than updated — the
unique constraint means both cannot exist, and the row that was already stored
under the correct label is the one someone typed most recently.

Data-only; no schema change, and safe to re-run (a second run finds no
truncated keys left to map).
"""
from alembic import op

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None

# Both patterns, spelled exactly as they go inside a Postgres E'' literal —
# the backslashes are doubled because an E-string eats a lone one, which would
# turn the literal \( into a capture group and quietly match the wrong thing.
# The new one is frozen here on purpose: this migration is the history of one
# specific re-key. test_crm_rate_plan_nested_tag asserts crm_filters still
# spells it this way, so changing the extraction again fails loudly instead of
# leaving these keys stranded.
_OLD = r"\\(([^)]+)\\)"
_NEW = r"\\(((?:[^()]|\\([^()]*\\))+)\\)"

# Old label -> new label, for every room_type where the two disagree. Only
# reservations whose rate_plan_name is blank reach the room_type fallback, so
# the mapping is built from exactly those.
_MAPPING_CTE = f"""
    WITH mapping AS (
        SELECT DISTINCT
               trim(substring(room_type from E'{_OLD}')) AS old_label,
               trim(substring(room_type from E'{_NEW}')) AS new_label
        FROM reservations
        WHERE coalesce(trim(rate_plan_name), '') = ''
          AND room_type IS NOT NULL
          AND substring(room_type from E'{_OLD}') IS DISTINCT FROM
              substring(room_type from E'{_NEW}')
    )
"""


def upgrade():
    conn = op.get_bind()

    # Drop a label that would collide with one already stored correctly.
    conn.exec_driver_sql(f"""
        {_MAPPING_CTE}
        DELETE FROM rate_plan_campaigns c
        USING mapping m
        WHERE c.rate_plan_name = m.old_label
          AND EXISTS (
              SELECT 1 FROM rate_plan_campaigns c2
              WHERE c2.rate_plan_name = m.new_label
          )
    """)

    conn.exec_driver_sql(f"""
        {_MAPPING_CTE}
        UPDATE rate_plan_campaigns c
        SET rate_plan_name = m.new_label,
            updated_at = now()
        FROM mapping m
        WHERE c.rate_plan_name = m.old_label
    """)


def downgrade():
    """Re-truncate the keys, so the labels attach again under the old pattern."""
    conn = op.get_bind()

    conn.exec_driver_sql(f"""
        {_MAPPING_CTE}
        DELETE FROM rate_plan_campaigns c
        USING mapping m
        WHERE c.rate_plan_name = m.new_label
          AND EXISTS (
              SELECT 1 FROM rate_plan_campaigns c2
              WHERE c2.rate_plan_name = m.old_label
          )
    """)

    conn.exec_driver_sql(f"""
        {_MAPPING_CTE}
        UPDATE rate_plan_campaigns c
        SET rate_plan_name = m.old_label,
            updated_at = now()
        FROM mapping m
        WHERE c.rate_plan_name = m.new_label
    """)

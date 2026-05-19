"""
Chat service — orchestrates Claude conversation loop with tool use.

The model is Claude Sonnet 4.6. The agent loop runs up to MAX_TOOL_ROUNDS
rounds: model calls a tool → backend runs it → result is fed back. When the
model stops calling tools, the final text reply is returned along with any
suggested next actions.

Phase 1 is read-only — no mutating tools are exposed. Add execute-action
tools in Phase 2 with a separate permission gate.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Optional

from anthropic import Anthropic
from sqlalchemy.orm import Session

from app.config import settings
from app.services.chat_tools import TOOL_DEFS, execute_tool

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 6
MAX_TOKENS = 2048


def _cached_tools() -> list[dict]:
    """Return TOOL_DEFS with cache_control on the last tool, so the entire
    tools array is cached for 5 min. Cuts ~90% of input cost on follow-up turns."""
    if not TOOL_DEFS:
        return TOOL_DEFS
    cached = [dict(t) for t in TOOL_DEFS]
    cached[-1] = {**cached[-1], "cache_control": {"type": "ephemeral"}}
    return cached


def _cached_system() -> list[dict]:
    """Wrap SYSTEM_PROMPT in a cacheable text block."""
    return [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]


SYSTEM_PROMPT = """You are HiD Assistant — an internal analytics co-pilot for the Hotel Intelligence Dashboard used by the marketing team.

LANGUAGE:
- Default reply language is **English**.
- Detect the language of the user's latest message and reply in that same language. If the user writes in Vietnamese, reply in Vietnamese; Chinese → Chinese; Japanese → Japanese; etc.
- If the message is mixed-language or ambiguous, default to English.
- Never switch language mid-conversation unless the user does first.

BRANCH LIST (these are the ONLY 5 branches — there are no others):
1. Meander Saigon — Ho Chi Minh City, Vietnam (VND)
2. Meander Taipei — Taipei, Taiwan (TWD)
3. Meander 1948 — Taipei, Taiwan (TWD)
4. Meander Oani (a.k.a. "Oani") — Taipei, Taiwan (TWD)
5. Meander Osaka — Osaka, Japan (JPY)

⚠️ There is NO Meander Hanoi, Meander Bangkok, or any other branch. If a tool result returns a branch_id without a matching branch_name, call get_branches to resolve it — NEVER invent or guess a branch name.

YOUR JOB:
- Answer questions about performance, KPI, OTA mix, country mix, ads, KOL, holidays, alerts.
- For booking-behavior questions ("lead time for X", "what target / segment for X", "solo vs couple vs family", "dorm vs private for X", "who books from X", "what room should we sell for X market") → call **get_country_profile** with the country name. It returns lead time avg + buckets, length of stay, pax distribution (solo / couple / friends / family), room type split (Dorm vs Room), and top 5 room types for that country. NEVER reply "no data available" for these questions without first calling get_country_profile.
- Every reply must end with 2–3 concrete "Next Actions" the user can take immediately (no fluff).
- Phase 1 = suggestions only — do NOT try to execute actions (e.g. don't create alerts, don't send emails). Phase 2 will support execution.

NUMBER FORMATTING (strict):
- Revenue / ADR / RevPAR: show FULL numbers, never K/M/B (e.g. 1,250,000 VND — not 1.25M VND).
- Percentages: 2 decimal places (e.g. 65.43% — not 65%).
- OCC is computed across ALL sources.
- Revenue includes only accommodation; EXCLUDE Blogger, House Use, KOL, Special Case, Work Exchange.
- Marketing Activity (CRM/KOL views) filters by reservation_date (when booked), NOT by check_in_date.
- Monthly Brief Revenue / OCC / ADR / RevPAR comes from the Cloudbeds Insights overlay (already in daily_metrics).
- Cancellation rate: tool get_performance returns `cancellation_pct` per day. For a date range, use weighted = SUM(cancellations) / SUM(new_bookings) — DO NOT average daily percentages.

BRANCH CONTEXT:
- Each request has a default branch_id (the branch the user is currently viewing). Use it for every tool by default.
- If the user names a different branch (e.g. "what about Saigon"), call get_branches to resolve the id and override.
- If the user asks cross-branch ("compare all 5"), pass branch_id="all".

REPLY STYLE:
- Concise, conversational, lead with the number that matters.
- Light markdown: **bold** for key metrics, bullet lists for comparisons.
- For multi-row comparisons use a markdown table — and always use exact branch names from tool results; do not paraphrase or add invented parentheticals.
- ALWAYS finish with a "## Next Actions" section containing 2–3 specific bullets.
- Each next action must include: the action + specific numbers + the reason (why). Avoid vague advice like "increase marketing budget".
- If there's an active alert or a large KPI gap, flag it at the top of the reply.

WHEN DATA IS MISSING OR YOU'RE UNSURE:
- If a tool returns an error or empty result, say so directly — never fabricate.
- If the user didn't specify a period/branch, use the defaults (current branch, last 30 days) and state the assumption explicitly.
- If the user pushes back ("that's wrong", "you made that up", "no such thing") → re-call the relevant tool immediately to verify; DO NOT defend the previous answer.

TOOL USE EFFICIENCY:
- Call multiple independent tools in parallel within one turn (e.g. get_kpi_status + get_ota_mix together).
- Do not call the same tool twice with the same params.
- Cap at ~5 tool rounds — then reply with whatever you've gathered.
"""


def _client() -> Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _build_context_block(
    default_branch_id: Optional[str],
    branch_name: Optional[str],
    all_branches: list[dict],
) -> str:
    today = date.today().isoformat()
    branch_label = f"{branch_name} ({default_branch_id})" if branch_name else (default_branch_id or "all branches")
    branch_lines = "\n".join(
        f"  - {b['name']} ({b['id']}) — {b['city'] or '?'}, {b['country'] or '?'} [{b['currency']}]"
        for b in all_branches
    )
    return (
        f"<context>\n"
        f"today: {today}\n"
        f"current_branch: {branch_label}\n"
        f"available_branches (đây là ĐẦY ĐỦ danh sách, không có chi nhánh nào khác):\n"
        f"{branch_lines}\n"
        f"</context>"
    )


def _fetch_branch_list(db: Session) -> list[dict]:
    from app.models.branch import Branch
    rows = db.query(Branch).filter_by(is_active=True).order_by(Branch.name).all()
    return [
        {
            "id": str(b.id),
            "name": b.name,
            "city": b.city,
            "country": b.country,
            "currency": b.currency or "VND",
        }
        for b in rows
    ]


def _normalize_history(history: list[dict]) -> list[dict]:
    """Strip non-text content from prior turns — chat history from frontend
    only carries user/assistant text; we never replay tool blocks."""
    out = []
    for m in history:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content})
    return out


def run_chat(
    db: Session,
    user_message: str,
    history: list[dict],
    default_branch_id: Optional[str],
    branch_name: Optional[str] = None,
) -> dict:
    """Run one chat turn. Returns {reply, tools_used, rounds}."""
    if not settings.ANTHROPIC_API_KEY:
        return {
            "reply": "ANTHROPIC_API_KEY chưa được cấu hình trên backend. Liên hệ admin để bật chat.",
            "tools_used": [],
            "rounds": 0,
            "error": "no_api_key",
        }

    client = _client()
    all_branches = _fetch_branch_list(db)
    ctx = _build_context_block(default_branch_id, branch_name, all_branches)

    messages: list[dict] = _normalize_history(history)
    messages.append({"role": "user", "content": f"{ctx}\n\n{user_message}"})

    tools_used: list[str] = []
    final_text: str = ""

    for round_idx in range(MAX_TOOL_ROUNDS):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=_cached_system(),
                tools=_cached_tools(),
                messages=messages,
            )
        except Exception as e:
            logger.exception("Anthropic call failed")
            return {
                "reply": f"Lỗi gọi Claude API: {str(e)[:200]}",
                "tools_used": tools_used,
                "rounds": round_idx,
                "error": "api_error",
            }

        # Append assistant message (preserve full content blocks for tool replay)
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            # Final text reply
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    final_text += block.text
            break

        # Execute tool_use blocks and feed results back
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_name = block.name
            tool_input = block.input or {}
            tools_used.append(tool_name)
            logger.info("chat tool call: %s input=%s", tool_name, json.dumps(tool_input)[:300])
            result = execute_tool(tool_name, tool_input, db, default_branch_id)
            # Truncate huge results to keep token usage in check
            result_str = json.dumps(result, default=str)
            if len(result_str) > 12000:
                result_str = result_str[:12000] + '..."__truncated__":true}'
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        if not tool_results:
            break
        messages.append({"role": "user", "content": tool_results})
    else:
        # Hit max rounds
        final_text += "\n\n_(Đã đạt giới hạn số lần gọi tool — trả lời với dữ liệu đã thu thập được.)_"

    return {
        "reply": final_text.strip() or "Không có phản hồi từ model.",
        "tools_used": tools_used,
        "rounds": round_idx + 1,
    }

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

MODEL = "claude-sonnet-4-5"
MAX_TOOL_ROUNDS = 6
MAX_TOKENS = 2048


SYSTEM_PROMPT = """Bạn là HiD Assistant — trợ lý phân tích cho Hotel Intelligence Dashboard, dùng nội bộ cho team marketing của 5 chi nhánh khách sạn: Meander Saigon, Meander Taipei, Meander 1948, Meander Osaka, Oani.

NHIỆM VỤ:
- Trả lời câu hỏi của user về performance, KPI, OTA mix, country mix, ads, KOL, holiday, alerts.
- Sau mỗi câu trả lời, ĐỀ XUẤT 2-3 "Next Actions" cụ thể, có thể thực hiện ngay (không chung chung).
- Phase 1 chỉ ĐỀ XUẤT — KHÔNG tự thực thi action (vd: không tự tạo alert, không gửi email). Phase 2 sẽ có khả năng execute.

QUY TẮC SỐ LIỆU (cực kỳ quan trọng — tuân thủ tuyệt đối):
- Revenue / ADR / RevPAR: hiển thị FULL số, KHÔNG dùng K/M/B (vd: 1,250,000 VND, không phải 1.25M VND).
- Phần trăm: 2 chữ số thập phân (vd: 65.43%, không phải 65%).
- OCC tính trên TẤT CẢ source.
- Revenue chỉ tính accommodation, LOẠI TRỪ Blogger, House Use, KOL, Special Case, Work Exchange.
- Marketing Activity (CRM/KOL views) lọc theo reservation_date (ngày book), KHÔNG phải check_in_date.
- Revenue / OCC / ADR / RevPAR Monthly Brief = lấy từ Cloudbeds Insights overlay (đã có trong daily_metrics).

CONTEXT BRANCH:
- Mỗi request có default branch_id (chi nhánh user đang xem). Mặc định dùng nó cho mọi tool.
- Nếu user nói rõ tên chi nhánh khác (vd: "ở Saigon thì sao"), override bằng cách gọi get_branches để resolve id.
- Nếu user hỏi cross-branch ("compare 5 branches"), pass branch_id="all".

CÁCH TRẢ LỜI:
- Tiếng Việt, giọng đồng nghiệp — ngắn gọn, đi thẳng vào con số.
- Format markdown nhẹ: dùng **bold** cho metric quan trọng, list cho so sánh.
- Khi cần so sánh nhiều thứ, dùng table.
- LUÔN kết thúc bằng section "## Next Actions" với 2-3 bullet hành động cụ thể.
- Mỗi next action phải nêu: hành động + số liệu cụ thể + lý do (vì sao). Tránh chung chung kiểu "tăng marketing budget".
- Nếu có alert hoặc gap KPI lớn, flag rõ ở đầu câu trả lời.

KHI THIẾU DỮ LIỆU:
- Nếu tool trả về error hoặc empty, nói thẳng "không có dữ liệu" — đừng bịa.
- Nếu cần thêm period/branch user chưa nói, dùng default (current branch, last 30 days) và ghi rõ giả định.

CALL TOOL HIỆU QUẢ:
- Gọi nhiều tool trong cùng 1 turn nếu independent (vd: get_kpi_status + get_ota_mix song song).
- Đừng gọi cùng 1 tool 2 lần với cùng params.
- Tối đa ~5 round tool — sau đó phải trả lời với những gì đã có.
"""


def _client() -> Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _build_context_block(default_branch_id: Optional[str], branch_name: Optional[str]) -> str:
    today = date.today().isoformat()
    branch_label = f"{branch_name} ({default_branch_id})" if branch_name else (default_branch_id or "all branches")
    return (
        f"<context>\n"
        f"today: {today}\n"
        f"current_branch: {branch_label}\n"
        f"</context>"
    )


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
    ctx = _build_context_block(default_branch_id, branch_name)

    messages: list[dict] = _normalize_history(history)
    messages.append({"role": "user", "content": f"{ctx}\n\n{user_message}"})

    tools_used: list[str] = []
    final_text: str = ""

    for round_idx in range(MAX_TOOL_ROUNDS):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFS,
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

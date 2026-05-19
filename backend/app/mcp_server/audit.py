"""MCP audit log writer — one row per tool invocation.

Best-effort: a failed audit write never propagates to the tool caller. We'd
rather lose an audit row than a tool response."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.mcp_audit_log import McpAuditLog
from app.models.user import User

logger = logging.getLogger(__name__)


def record(
    user: Optional[User],
    tool_name: str,
    arguments: dict,
    status: str,
    duration_ms: int,
    response: Any = None,
    error_message: Optional[str] = None,
) -> None:
    """Write one mcp_audit_log row. Swallows all exceptions."""
    db: Optional[Session] = None
    try:
        size = None
        if response is not None:
            try:
                size = len(json.dumps(response, default=str).encode())
            except Exception:
                size = None
        db = SessionLocal()
        row = McpAuditLog(
            user_id=user.id if user else None,
            tool_name=tool_name,
            arguments=arguments or {},
            status=status,
            response_size_bytes=size,
            duration_ms=duration_ms,
            error_message=error_message[:1000] if error_message else None,
        )
        db.add(row)
        db.commit()
    except Exception as e:
        logger.warning("mcp_audit_log write failed: %s", e)
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.database import Base


class McpAuditLog(Base):
    __tablename__ = "mcp_audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Legacy: pre-OAuth API-key callers. Kept nullable for backward compat —
    # the API-key MCP path was removed in 045 in favour of OAuth.
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    # Current: OAuth-authenticated HiD user that made the call.
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tool_name = Column(String(64), nullable=False)
    arguments = Column(JSONB, nullable=True)
    status = Column(String(16), nullable=False)  # 'ok' | 'denied' | 'error'
    response_size_bytes = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

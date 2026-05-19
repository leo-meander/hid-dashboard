import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.database import Base


class McpAuditLog(Base):
    __tablename__ = "mcp_audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    tool_name = Column(String(64), nullable=False)
    arguments = Column(JSONB, nullable=True)
    status = Column(String(16), nullable=False)  # 'ok' | 'denied' | 'error'
    response_size_bytes = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

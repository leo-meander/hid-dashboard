import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Boolean, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    key_hash = Column(String(200), nullable=False)
    key_prefix = Column(String(12), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    # MCP scoping — null/[] = no access; ["*"] = all; ["uuid",...] / ["tool",...] = whitelist.
    # Default-deny so existing keys cannot reach /mcp until an admin grants scopes.
    allowed_branches = Column(JSONB, nullable=True)
    allowed_tools = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    creator = relationship("User", foreign_keys=[created_by])

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Boolean, String, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(200), unique=True, nullable=False)
    name = Column(String(100), nullable=True)
    role = Column(String(20), default="editor")  # admin, editor, viewer
    password_hash = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True)
    # Access scope — NULL/empty means "all" (full access). Admins ignore both.
    allowed_branches = Column(ARRAY(Text), nullable=True)  # branch UUIDs (as text)
    allowed_pages = Column(ARRAY(Text), nullable=True)     # sidebar group keys
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

"""
Chat router — POST /api/chat for HiD Assistant.

Phase 1: read-only Q&A over hotel data with Claude tool-use. No mutating
actions yet; the model only suggests Next Actions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.branch import Branch
from app.services.chat_service import MODEL as CHAT_MODEL, run_chat

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list)
    branch_id: Optional[str] = None  # 'all' | UUID string | null


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("")
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    bid = payload.branch_id
    if bid and bid.lower() != "all":
        try:
            UUID(bid)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid branch_id")

    branch_name = None
    if bid and bid.lower() != "all":
        b = db.query(Branch).filter_by(id=bid, is_active=True).first()
        if b:
            branch_name = b.name

    default_branch = None if (not bid or bid.lower() == "all") else bid

    result = run_chat(
        db=db,
        user_message=payload.message,
        history=[m.dict() for m in payload.history[-20:]],
        default_branch_id=default_branch,
        branch_name=branch_name,
    )
    return _envelope(result)


@router.get("/health")
def chat_health():
    """Returns whether the chat feature is configured (API key present)."""
    from app.config import settings
    return _envelope({
        "configured": bool(settings.ANTHROPIC_API_KEY),
        "model": CHAT_MODEL,
    })

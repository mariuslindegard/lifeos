from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    password: str


class RawEntryCreate(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    occurred_at: datetime | None = None
    source: str = "web"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RawEntryOut(BaseModel):
    id: int
    text: str
    source: str
    occurred_at: datetime
    processing_status: str
    created_at: datetime


class EventOut(BaseModel):
    id: int
    raw_entry_id: int
    event_type: str
    occurred_at: datetime
    summary: str
    attributes: dict[str, Any]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: int | None = None


class ChatResponse(BaseModel):
    answer: str
    session_id: int | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)


class SnoozeRequest(BaseModel):
    days: int = Field(default=1, ge=1, le=30)

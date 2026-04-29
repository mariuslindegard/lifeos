import hashlib
import math
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import desc, text
from sqlalchemy.orm import Session

from lifeos.config import settings
from lifeos.llm import fallback_embedding, get_llm
from lifeos.models import ChatMessage, Embedding, ExtractedEvent, Memory, RawEntry, TimeItem


def vector_for_text(content: str) -> list[float]:
    try:
        return get_llm().embed(content)
    except Exception:
        return fallback_embedding(content)


def upsert_embedding(db: Session, object_type: str, object_id: int, content: str) -> None:
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    existing = (
        db.query(Embedding)
        .filter(
            Embedding.object_type == object_type,
            Embedding.object_id == object_id,
            Embedding.model == settings.ollama_embed_model,
        )
        .one_or_none()
    )
    if existing and existing.content_hash == content_hash:
        return
    vector = vector_for_text(content)
    if existing:
        existing.content_hash = content_hash
        existing.vector = vector
    else:
        db.add(
            Embedding(
                object_type=object_type,
                object_id=object_id,
                model=settings.ollama_embed_model,
                content_hash=content_hash,
                vector=vector,
            )
        )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    length = min(len(left), len(right))
    if length == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(length))
    left_mag = math.sqrt(sum(value * value for value in left)) or 1.0
    right_mag = math.sqrt(sum(value * value for value in right)) or 1.0
    return dot / (left_mag * right_mag)


def semantic_search(db: Session, query: str, *, limit: int = 8) -> list[dict]:
    query_vector = vector_for_text(query)
    scored: list[tuple[float, Embedding]] = []
    for embedding in db.query(Embedding).all():
        scored.append((cosine_similarity(query_vector, embedding.vector), embedding))
    scored.sort(key=lambda item: item[0], reverse=True)

    results = []
    for score, embedding in scored[:limit]:
        content = None
        if embedding.object_type == "raw_entry":
            entry = db.get(RawEntry, embedding.object_id)
            content = entry.text if entry else None
        elif embedding.object_type == "memory":
            memory = db.get(Memory, embedding.object_id)
            content = memory.content if memory else None
        elif embedding.object_type == "event":
            event = db.get(ExtractedEvent, embedding.object_id)
            content = event.summary if event else None
        if content:
            results.append(
                {
                    "object_type": embedding.object_type,
                    "object_id": embedding.object_id,
                    "score": round(score, 4),
                    "content": content,
                }
            )
    return results


def text_search(db: Session, query: str, *, limit: int = 10) -> list[RawEntry]:
    escaped = " ".join(part.replace('"', "") for part in query.split())
    if not escaped:
        return []
    rows = db.execute(
        text(
            "SELECT raw_entries.* FROM raw_entries_fts "
            "JOIN raw_entries ON raw_entries_fts.rowid = raw_entries.id "
            "WHERE raw_entries_fts MATCH :query "
            "ORDER BY rank LIMIT :limit"
        ),
        {"query": escaped, "limit": limit},
    ).mappings()
    ids = [row["id"] for row in rows]
    if not ids:
        return []
    entries = db.query(RawEntry).filter(RawEntry.id.in_(ids)).all()
    by_id = {entry.id: entry for entry in entries}
    return [by_id[entry_id] for entry_id in ids if entry_id in by_id]


def parse_date_range(message: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    now = now or datetime.now(timezone.utc)
    lower = message.lower()
    if "today" in lower:
        target = now.date()
    elif "yesterday" in lower:
        target = (now - timedelta(days=1)).date()
    else:
        match = __import__("re").search(r"\b(20\d{2}-\d{2}-\d{2})\b", message)
        if not match:
            return None
        target = datetime.fromisoformat(match.group(1)).date()
    start = datetime.combine(target, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def events_between(db: Session, start: datetime, end: datetime) -> list[ExtractedEvent]:
    return (
        db.query(ExtractedEvent)
        .filter(ExtractedEvent.occurred_at >= start, ExtractedEvent.occurred_at < end)
        .order_by(ExtractedEvent.occurred_at)
        .all()
    )


def recent_context(db: Session) -> dict:
    entries = db.query(RawEntry).order_by(desc(RawEntry.occurred_at)).limit(20).all()
    time_items = db.query(TimeItem).filter(TimeItem.status.in_(("open", "snoozed"))).order_by(TimeItem.due_at).limit(20).all()
    chat_messages = db.query(ChatMessage).order_by(desc(ChatMessage.created_at)).limit(12).all()
    memories = (
        db.query(Memory)
        .filter(Memory.superseded_by_id.is_(None))
        .order_by(desc(Memory.confidence), desc(Memory.updated_at))
        .limit(20)
        .all()
    )
    return {
        "entries": [
            {"id": entry.id, "text": entry.text, "occurred_at": entry.occurred_at.isoformat()}
            for entry in entries
        ],
        "time_items": [
            {
                "id": item.id,
                "kind": item.kind,
                "title": item.title,
                "status": item.status,
                "due_at": item.due_at.isoformat() if item.due_at else None,
            }
            for item in time_items
        ],
        "chat_messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at.isoformat(),
            }
            for message in chat_messages
        ],
        "memories": [
            {"id": memory.id, "kind": memory.kind, "content": memory.content, "confidence": memory.confidence}
            for memory in memories
        ],
    }

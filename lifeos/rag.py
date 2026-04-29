import calendar
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import desc, or_, text
from sqlalchemy.orm import Session

from lifeos.config import settings
from lifeos.llm import fallback_embedding, get_llm
from lifeos.models import ChatMessage, Embedding, ExtractedEvent, Memory, RawEntry, ReflectionSummary, TimeItem

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True)
class ResolvedTimeWindow:
    kind: str
    label: str
    start_utc: datetime
    end_utc: datetime
    start_local_date: date
    end_local_date: date
    is_single_day: bool


@dataclass(frozen=True)
class HistoricalContext:
    window: ResolvedTimeWindow
    logs: list[RawEntry]
    events: list[ExtractedEvent]
    time_items: list[TimeItem]
    chat_messages: list[ChatMessage]
    reflection_summaries: list[ReflectionSummary]
    memories: list[Memory]


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


def local_tz(timezone_name: str | None = None) -> ZoneInfo:
    return ZoneInfo(timezone_name or settings.default_timezone)


def local_date_bounds(day_value: date, timezone_name: str | None = None) -> tuple[datetime, datetime]:
    tz = local_tz(timezone_name)
    start = datetime.combine(day_value, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def local_window_bounds(start_day: date, end_day: date, timezone_name: str | None = None) -> tuple[datetime, datetime]:
    start, _discard = local_date_bounds(start_day, timezone_name)
    _discard, end = local_date_bounds(end_day, timezone_name)
    return start, end


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def shift_months(day_value: date, months: int) -> date:
    year = day_value.year
    month = day_value.month - months
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    day = min(day_value.day, last_day_of_month(year, month))
    return date(year, month, day)


def build_window(
    *,
    kind: str,
    label: str,
    start_local_date: date,
    end_local_date: date,
    timezone_name: str | None = None,
) -> ResolvedTimeWindow:
    start_utc, end_utc = local_window_bounds(start_local_date, end_local_date, timezone_name)
    return ResolvedTimeWindow(
        kind=kind,
        label=label,
        start_utc=start_utc,
        end_utc=end_utc,
        start_local_date=start_local_date,
        end_local_date=end_local_date,
        is_single_day=start_local_date == end_local_date,
    )


def parse_explicit_date(fragment: str, base_local_date: date) -> date | None:
    fragment = fragment.strip().lower()
    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", fragment)
    if iso_match:
        return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
    month_match = re.search(
        r"\b("
        + "|".join(MONTHS)
        + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?\b",
        fragment,
    )
    if not month_match:
        return None
    month = MONTHS[month_match.group(1)]
    day_value = int(month_match.group(2))
    year = int(month_match.group(3) or base_local_date.year)
    return date(year, month, day_value)


def parse_named_window(fragment: str, base_local_date: date, timezone_name: str | None = None) -> ResolvedTimeWindow | None:
    lower = fragment.strip().lower()
    weekday = base_local_date.weekday()
    week_start = base_local_date - timedelta(days=weekday)
    if "today" in lower:
        return build_window(
            kind="day",
            label=f"Today ({base_local_date.isoformat()})",
            start_local_date=base_local_date,
            end_local_date=base_local_date,
            timezone_name=timezone_name,
        )
    if "yesterday" in lower:
        target = base_local_date - timedelta(days=1)
        return build_window(
            kind="day",
            label=f"Yesterday ({target.isoformat()})",
            start_local_date=target,
            end_local_date=target,
            timezone_name=timezone_name,
        )
    if lower == "this week" or " this week" in lower:
        return build_window(
            kind="week",
            label="This week",
            start_local_date=week_start,
            end_local_date=base_local_date,
            timezone_name=timezone_name,
        )
    if "last week" in lower:
        start = week_start - timedelta(days=7)
        end = week_start - timedelta(days=1)
        return build_window(kind="week", label="Last week", start_local_date=start, end_local_date=end, timezone_name=timezone_name)
    month_start = base_local_date.replace(day=1)
    if lower == "this month" or " this month" in lower:
        return build_window(
            kind="month",
            label="This month",
            start_local_date=month_start,
            end_local_date=base_local_date,
            timezone_name=timezone_name,
        )
    if "last month" in lower:
        prev_month_end = month_start - timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)
        return build_window(
            kind="month",
            label="Last month",
            start_local_date=prev_month_start,
            end_local_date=prev_month_end,
            timezone_name=timezone_name,
        )
    past_days = re.search(r"\bpast\s+(\d{1,3})\s+days?\b", lower)
    if past_days:
        count = max(1, int(past_days.group(1)))
        start = base_local_date - timedelta(days=count - 1)
        return build_window(
            kind="rolling_days",
            label=f"Past {count} days",
            start_local_date=start,
            end_local_date=base_local_date,
            timezone_name=timezone_name,
        )
    return None


def parse_relative_single_day(fragment: str, base_local_date: date, timezone_name: str | None = None) -> ResolvedTimeWindow | None:
    lower = fragment.strip().lower()
    day_match = re.search(r"\b(\d{1,3})\s+days?\s+ago\b", lower)
    if day_match:
        offset = int(day_match.group(1))
        target = base_local_date - timedelta(days=offset)
        return build_window(
            kind="relative_day",
            label=f"{offset} days ago ({target.isoformat()})",
            start_local_date=target,
            end_local_date=target,
            timezone_name=timezone_name,
        )
    week_match = re.search(r"\b(\d{1,3})\s+weeks?\s+ago\b", lower)
    if week_match:
        offset = int(week_match.group(1))
        target = base_local_date - timedelta(days=offset * 7)
        return build_window(
            kind="relative_day",
            label=f"{offset} weeks ago ({target.isoformat()})",
            start_local_date=target,
            end_local_date=target,
            timezone_name=timezone_name,
        )
    month_match = re.search(r"\b(\d{1,3})\s+months?\s+ago\b", lower)
    if month_match:
        offset = int(month_match.group(1))
        target = shift_months(base_local_date, offset)
        return build_window(
            kind="relative_day",
            label=f"{offset} months ago ({target.isoformat()})",
            start_local_date=target,
            end_local_date=target,
            timezone_name=timezone_name,
        )
    return None


def parse_time_reference(
    fragment: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> ResolvedTimeWindow | None:
    now = now or datetime.now(timezone.utc)
    base_local_date = now.astimezone(local_tz(timezone_name)).date()

    explicit_date = parse_explicit_date(fragment, base_local_date)
    if explicit_date:
        return build_window(
            kind="day",
            label=explicit_date.isoformat(),
            start_local_date=explicit_date,
            end_local_date=explicit_date,
            timezone_name=timezone_name,
        )

    named_window = parse_named_window(fragment, base_local_date, timezone_name)
    if named_window:
        return named_window

    return parse_relative_single_day(fragment, base_local_date, timezone_name)


def parse_comparison_time_windows(
    message: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> tuple[ResolvedTimeWindow, ResolvedTimeWindow] | None:
    lower = message.lower()
    match = re.search(r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:\?|$)", lower)
    if not match:
        return None
    left = parse_time_reference(match.group(1), now=now, timezone_name=timezone_name)
    right = parse_time_reference(match.group(2), now=now, timezone_name=timezone_name)
    if left and right:
        return left, right
    return None


def parse_time_window(
    message: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> ResolvedTimeWindow | None:
    now = now or datetime.now(timezone.utc)
    base_local_date = now.astimezone(local_tz(timezone_name)).date()
    lower = message.lower()

    comparison = parse_comparison_time_windows(message, now=now, timezone_name=timezone_name)
    if comparison:
        start_local_date = min(comparison[0].start_local_date, comparison[1].start_local_date)
        end_local_date = max(comparison[0].end_local_date, comparison[1].end_local_date)
        return build_window(
            kind="bounded_range",
            label=f"{comparison[0].label} through {comparison[1].label}",
            start_local_date=start_local_date,
            end_local_date=end_local_date,
            timezone_name=timezone_name,
        )

    explicit_range = re.search(r"\b(?:between|from)\s+(.+?)\s+(?:and|to)\s+(.+?)(?:\?|$)", message, re.I)
    if explicit_range:
        start_ref = parse_time_reference(explicit_range.group(1), now=now, timezone_name=timezone_name)
        end_ref = parse_time_reference(explicit_range.group(2), now=now, timezone_name=timezone_name)
        if start_ref and end_ref:
            return build_window(
                kind="bounded_range",
                label=f"{start_ref.label} through {end_ref.label}",
                start_local_date=min(start_ref.start_local_date, end_ref.start_local_date),
                end_local_date=max(start_ref.end_local_date, end_ref.end_local_date),
                timezone_name=timezone_name,
            )

    direct = parse_time_reference(message, now=now, timezone_name=timezone_name)
    if direct:
        return direct

    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", lower)
    if iso_match:
        target = datetime.fromisoformat(iso_match.group(1)).date()
        return build_window(
            kind="day",
            label=target.isoformat(),
            start_local_date=target,
            end_local_date=target,
            timezone_name=timezone_name,
        )

    month_range = re.search(
        r"\bbetween\s+([a-z]+\s+\d{1,2}(?:,\s*20\d{2})?)\s+and\s+([a-z]+\s+\d{1,2}(?:,\s*20\d{2})?)",
        lower,
    )
    if month_range:
        left_date = parse_explicit_date(month_range.group(1), base_local_date)
        right_date = parse_explicit_date(month_range.group(2), base_local_date)
        if left_date and right_date:
            return build_window(
                kind="bounded_range",
                label=f"{left_date.isoformat()} through {right_date.isoformat()}",
                start_local_date=min(left_date, right_date),
                end_local_date=max(left_date, right_date),
                timezone_name=timezone_name,
            )
    return None


def parse_date_range(
    message: str,
    now: datetime | None = None,
    *,
    timezone_name: str | None = None,
) -> tuple[datetime, datetime] | None:
    window = parse_time_window(message, now=now, timezone_name=timezone_name)
    if not window:
        return None
    return window.start_utc, window.end_utc


def events_between(db: Session, start: datetime, end: datetime) -> list[ExtractedEvent]:
    return (
        db.query(ExtractedEvent)
        .filter(ExtractedEvent.occurred_at >= start, ExtractedEvent.occurred_at < end)
        .order_by(ExtractedEvent.occurred_at)
        .all()
    )


def historical_context(
    db: Session,
    window: ResolvedTimeWindow,
    *,
    include_memories: bool = False,
) -> HistoricalContext:
    logs = (
        db.query(RawEntry)
        .filter(RawEntry.occurred_at >= window.start_utc, RawEntry.occurred_at < window.end_utc)
        .order_by(RawEntry.occurred_at)
        .all()
    )
    events = (
        db.query(ExtractedEvent)
        .filter(ExtractedEvent.occurred_at >= window.start_utc, ExtractedEvent.occurred_at < window.end_utc)
        .order_by(ExtractedEvent.occurred_at)
        .all()
    )
    time_items = (
        db.query(TimeItem)
        .filter(
            or_(
                (TimeItem.due_at >= window.start_utc) & (TimeItem.due_at < window.end_utc),
                (TimeItem.starts_at >= window.start_utc) & (TimeItem.starts_at < window.end_utc),
            )
        )
        .order_by(TimeItem.due_at, TimeItem.starts_at, TimeItem.created_at)
        .all()
    )
    chat_messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.created_at >= window.start_utc, ChatMessage.created_at < window.end_utc)
        .order_by(ChatMessage.created_at)
        .all()
    )
    reflection_summaries = (
        db.query(ReflectionSummary)
        .filter(
            ReflectionSummary.window_start < window.end_utc,
            ReflectionSummary.window_end > window.start_utc,
        )
        .order_by(ReflectionSummary.window_start, ReflectionSummary.created_at)
        .all()
    )
    memories: list[Memory] = []
    if include_memories:
        memories = (
            db.query(Memory)
            .filter(Memory.superseded_by_id.is_(None))
            .order_by(desc(Memory.confidence), desc(Memory.updated_at))
            .limit(24)
            .all()
        )
    return HistoricalContext(
        window=window,
        logs=logs,
        events=events,
        time_items=time_items,
        chat_messages=chat_messages,
        reflection_summaries=reflection_summaries,
        memories=memories,
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

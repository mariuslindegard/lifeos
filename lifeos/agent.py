import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, or_, text
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import Session

from lifeos.config import settings
from lifeos.llm import get_llm, safe_json_object
from lifeos.models import (
    AgentRun,
    ChatMessage,
    ChatSession,
    DailyReport,
    DashboardCard,
    ExtractedEvent,
    Memory,
    PersonaProfile,
    RawEntry,
    Recommendation,
    TimeItem,
)
from lifeos.rag import events_between, parse_date_range, recent_context, semantic_search, upsert_embedding

EVENT_TYPES = ("meal", "exercise", "sleep", "mood", "symptom", "work", "activity", "note")
TIME_ITEM_TYPES = ("task", "reminder", "deadline", "event", "time_block", "open_loop")
CARD_MODES = ("execution", "analysis", "journal", "persona")
CARD_ORDER = ("execution", "analysis", "journal", "persona")
ANALYSIS_MODES = ("daily_execution", "self_analysis", "life_journal", "persona_refresh")
ANALYSIS_VERSION = "v1"
PROMPT_VERSION = "v2"

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
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def local_tz() -> ZoneInfo:
    return ZoneInfo(settings.default_timezone)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=local_tz())
    return value.astimezone(timezone.utc)


def db_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def start_of_day(value: datetime) -> datetime:
    local_value = value.astimezone(local_tz())
    return datetime.combine(local_value.date(), time.min, tzinfo=local_tz()).astimezone(timezone.utc)


def end_of_day(value: datetime) -> datetime:
    return start_of_day(value) + timedelta(days=1)


def local_date(value: datetime | None) -> date | None:
    value = db_utc(value)
    return value.astimezone(local_tz()).date() if value else None


def is_session_active(session: ChatSession, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    last_message_at = db_utc(session.last_message_at or session.created_at)
    if not last_message_at:
        return True
    if now - last_message_at >= timedelta(hours=6):
        return False
    return local_date(last_message_at) == local_date(now)


def is_execution_relevant(text_value: str) -> bool:
    lower = text_value.lower()
    return bool(
        classify_time_item(text_value)
        or any(
            word in lower
            for word in (
                "selling",
                "work",
                "worked",
                "plan",
                "goal",
                "deadline",
                "blocked",
                "could've gone better",
                "could have gone better",
            )
        )
    )


def is_self_analysis_relevant(text_value: str) -> bool:
    lower = text_value.lower()
    return any(
        word in lower
        for word in (
            "ate",
            "food",
            "meal",
            "energized",
            "energy",
            "tired",
            "focus",
            "mood",
            "self-esteem",
            "confidence",
            "sleep",
            "felt",
            "crash",
            "anxious",
            "stress",
        )
    )


def is_persona_relevant(text_value: str) -> bool:
    lower = text_value.lower()
    return any(
        word in lower
        for word in (
            "i am",
            "i'm",
            "i prefer",
            "i like",
            "i dislike",
            "i usually",
            "i tend",
            "self-esteem",
            "confidence",
            "my goal",
            "my goals",
        )
    )


def applicable_analysis_modes(message: ChatMessage) -> list[str]:
    if message.role == "assistant":
        return ["life_journal"]
    modes = ["life_journal"]
    if is_execution_relevant(message.content):
        modes.append("daily_execution")
    if is_self_analysis_relevant(message.content):
        modes.append("self_analysis")
    if is_persona_relevant(message.content):
        modes.append("persona_refresh")
    return [mode for mode in ANALYSIS_MODES if mode in modes]


def analysis_metadata(message: ChatMessage) -> dict[str, Any]:
    metadata = dict(message.metadata_ or {})
    metadata.setdefault("analysis_modes", {})
    metadata.setdefault("applicable_modes", applicable_analysis_modes(message))
    return metadata


def set_message_analysis_coverage(
    message: ChatMessage,
    mode: str,
    *,
    error: str | None = None,
    analyzed_at: datetime | None = None,
) -> None:
    metadata = analysis_metadata(message)
    metadata["analysis_modes"][mode] = error is None
    metadata["applicable_modes"] = applicable_analysis_modes(message)
    message.metadata_ = metadata
    flag_modified(message, "metadata_")
    message.analysis_version = ANALYSIS_VERSION
    if error:
        message.analysis_status = "error"
        message.analysis_error = error
        return
    applicable = set(metadata["applicable_modes"])
    completed = {key for key, value in metadata["analysis_modes"].items() if value}
    message.analysis_status = "complete" if applicable <= completed else "partial"
    message.analysis_error = None
    if message.analysis_status == "complete":
        message.analyzed_at = analyzed_at or datetime.now(timezone.utc)


def pending_messages_for_mode(db: Session, mode: str, *, limit: int = 80) -> list[ChatMessage]:
    candidates = (
        db.query(ChatMessage)
        .filter(ChatMessage.analysis_status.in_(("pending", "partial", "error")))
        .order_by(ChatMessage.created_at)
        .limit(300)
        .all()
    )
    selected = []
    for message in candidates:
        metadata = analysis_metadata(message)
        if mode in metadata["applicable_modes"] and not metadata["analysis_modes"].get(mode):
            selected.append(message)
        if len(selected) >= limit:
            break
    return selected


def classify_event_type(text_value: str) -> str:
    lower = text_value.lower()
    if any(word in lower for word in ("ate", "meal", "breakfast", "lunch", "dinner", "coffee", "snack")):
        return "meal"
    if any(word in lower for word in ("run", "walk", "gym", "workout", "exercise", "lift")):
        return "exercise"
    if any(word in lower for word in ("slept", "sleep", "woke", "bed")):
        return "sleep"
    if any(word in lower for word in ("felt", "mood", "anxious", "happy", "sad", "energy", "focus")):
        return "mood"
    if any(word in lower for word in ("crash", "headache", "sick", "pain", "blood sugar", "craving")):
        return "symptom"
    if any(word in lower for word in ("worked", "meeting", "coded", "focus block", "deep work")):
        return "work"
    return "note"


def classify_time_item(text_value: str) -> str | None:
    lower = text_value.lower()
    if any(word in lower for word in ("deadline", "due by", "due on", "due ")):
        return "deadline"
    if any(word in lower for word in ("remind me", "reminder", "notify me")):
        return "reminder"
    if any(word in lower for word in ("time block", "deep work block", "focus block", "block ")):
        return "time_block"
    if any(word in lower for word in ("meeting", "appointment", "event", "call with")):
        return "event"
    if any(word in lower for word in ("todo", "task", "need to", "i need", "remember to")):
        return "task"
    if any(word in lower for word in ("figure out", "decide", "follow up", "open loop")):
        return "open_loop"
    return None


def parse_time_hint(text_value: str) -> tuple[int, int] | None:
    match = re.search(r"\b(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text_value.lower())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def parse_natural_datetime(text_value: str, base_time: datetime | None = None) -> datetime | None:
    base_time = base_time or datetime.now(timezone.utc)
    base_local = base_time.astimezone(local_tz())
    lower = text_value.lower()
    target_date: date | None = None

    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", lower)
    if iso_match:
        target_date = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))

    if target_date is None:
        month_match = re.search(
            r"\b("
            + "|".join(MONTHS)
            + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?\b",
            lower,
        )
        if month_match:
            month = MONTHS[month_match.group(1)]
            day = int(month_match.group(2))
            year = int(month_match.group(3) or base_local.year)
            candidate = date(year, month, day)
            if candidate < base_local.date() and not month_match.group(3):
                candidate = date(year + 1, month, day)
            target_date = candidate

    if target_date is None:
        if "tomorrow" in lower:
            target_date = base_local.date() + timedelta(days=1)
        elif "today" in lower:
            target_date = base_local.date()

    if target_date is None:
        for name, weekday in WEEKDAYS.items():
            if re.search(rf"\b{name}\b", lower):
                days = (weekday - base_local.weekday()) % 7
                days = 7 if days == 0 else days
                target_date = base_local.date() + timedelta(days=days)
                break

    if target_date is None:
        return None

    hour, minute = parse_time_hint(text_value) or (9, 0)
    return datetime.combine(target_date, time(hour, minute), tzinfo=local_tz()).astimezone(timezone.utc)


def clean_time_item_title(text_value: str) -> str:
    title = text_value.strip()
    title = re.sub(r"^\s*(remind me to|remind me|remember to|deadline for|deadline|todo:?|task:?|i need to|need to)\s+", "", title, flags=re.I)
    title = re.sub(r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "", title, flags=re.I)
    title = re.sub(r"\b(?:at\s*)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", "", title, flags=re.I)
    title = re.sub(r"\b(?:by|on|for)\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*20\d{2})?\b", "", title, flags=re.I)
    title = re.sub(r"^to\s+", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" .:-")
    return title[:240] or text_value.strip()[:240]


def extract_time_items_from_text(
    text_value: str,
    *,
    base_time: datetime | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
) -> list[dict[str, Any]]:
    kind = classify_time_item(text_value)
    if not kind:
        return []
    due_at = parse_natural_datetime(text_value, base_time=base_time)
    title = clean_time_item_title(text_value)
    priority = 80 if kind in {"deadline", "reminder"} else 60
    starts_at = due_at if kind in {"event", "time_block"} else None
    ends_at = starts_at + timedelta(hours=1) if starts_at and kind in {"event", "time_block"} else None
    return [
        {
            "kind": kind,
            "title": title,
            "status": "open",
            "priority": priority,
            "due_at": due_at,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "source_type": source_type,
            "source_id": source_id,
            "evidence": [{"object_type": source_type, "object_id": source_id}] if source_type and source_id else [],
            "attributes": {"extraction": "fallback"},
        }
    ]


def fallback_extract(entry: RawEntry) -> dict[str, Any]:
    return {
        "events": [
            {
                "event_type": classify_event_type(entry.text),
                "summary": entry.text[:500],
                "occurred_at": entry.occurred_at.isoformat(),
                "attributes": {"extraction": "fallback"},
            }
        ],
        "memories": [],
        "time_items": extract_time_items_from_text(
            entry.text,
            base_time=entry.occurred_at,
            source_type="raw_entry",
            source_id=entry.id,
        ),
    }


def extract_entry(entry: RawEntry) -> dict[str, Any]:
    prompt = (
        "Extract LifeOS structured data from this raw log. Return JSON only with keys "
        "events, memories, and time_items. events must contain event_type, summary, occurred_at, attributes. "
        "event_type must be one of: "
        f"{', '.join(EVENT_TYPES)}. memories should contain kind, content, confidence, attributes. "
        "time_items should contain kind, title, priority, due_at, starts_at, ends_at, attributes. "
        f"time item kind must be one of: {', '.join(TIME_ITEM_TYPES)}. "
        "Only create memories for durable preferences, traits, recurring patterns, goals, or health responses.\n\n"
        f"Timestamp: {entry.occurred_at.isoformat()}\n"
        f"Log: {entry.text}"
    )
    try:
        parsed = safe_json_object(get_llm().chat([{"role": "user", "content": prompt}], temperature=0.0))
    except Exception:
        parsed = {}
    if not parsed.get("events"):
        return fallback_extract(entry)
    if not parsed.get("time_items"):
        parsed["time_items"] = extract_time_items_from_text(
            entry.text,
            base_time=entry.occurred_at,
            source_type="raw_entry",
            source_id=entry.id,
        )
    return parsed


def parse_optional_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def add_time_item(
    db: Session,
    *,
    kind: str,
    title: str,
    notes: str | None = None,
    status: str = "open",
    priority: int = 50,
    due_at: datetime | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    evidence: list | None = None,
    attributes: dict | None = None,
) -> TimeItem:
    item = TimeItem(
        kind=kind if kind in TIME_ITEM_TYPES else "task",
        title=title.strip()[:240],
        notes=notes,
        status=status,
        priority=max(0, min(100, int(priority))),
        due_at=as_utc(due_at) if due_at else None,
        starts_at=as_utc(starts_at) if starts_at else None,
        ends_at=as_utc(ends_at) if ends_at else None,
        source_type=source_type,
        source_id=source_id,
        evidence=evidence or [],
        attributes=attributes or {},
    )
    db.add(item)
    db.flush()
    return item


def create_raw_entry(
    db: Session,
    *,
    text_value: str,
    source: str = "web",
    occurred_at: datetime | None = None,
    metadata: dict | None = None,
) -> RawEntry:
    entry = RawEntry(
        text=text_value.strip(),
        source=source,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        metadata_=metadata or {},
    )
    db.add(entry)
    db.flush()
    db.execute(text("INSERT INTO raw_entries_fts(rowid, text) VALUES (:id, :text)"), {"id": entry.id, "text": entry.text})
    upsert_embedding(db, "raw_entry", entry.id, entry.text)
    db.commit()
    db.refresh(entry)
    process_pending_entries(db, limit=1)
    refresh_dashboard_cards(db)
    db.refresh(entry)
    return entry


def process_pending_entries(db: Session, *, limit: int = 20) -> int:
    entries = (
        db.query(RawEntry)
        .filter(RawEntry.processing_status == "pending")
        .order_by(RawEntry.occurred_at)
        .limit(limit)
        .all()
    )
    for entry in entries:
        try:
            extracted = extract_entry(entry)
            for item in extracted.get("events", []):
                event_type = item.get("event_type") if item.get("event_type") in EVENT_TYPES else "note"
                occurred_at = parse_optional_datetime(item.get("occurred_at")) or entry.occurred_at
                event = ExtractedEvent(
                    raw_entry_id=entry.id,
                    event_type=event_type,
                    occurred_at=occurred_at,
                    summary=str(item.get("summary") or entry.text),
                    attributes=item.get("attributes") if isinstance(item.get("attributes"), dict) else {},
                )
                db.add(event)
                db.flush()
                upsert_embedding(db, "event", event.id, event.summary)

            for item in extracted.get("memories", []):
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                memory = add_memory(
                    db,
                    kind=str(item.get("kind") or "inferred"),
                    content=content,
                    confidence=float(item.get("confidence") or 0.5),
                    attributes=item.get("attributes") if isinstance(item.get("attributes"), dict) else {},
                    evidence=[{"raw_entry_id": entry.id}],
                )
                upsert_embedding(db, "memory", memory.id, memory.content)

            time_items = extracted.get("time_items") or []
            for item in time_items:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                add_time_item(
                    db,
                    kind=str(item.get("kind") or "task"),
                    title=title,
                    notes=item.get("notes"),
                    priority=int(item.get("priority") or 50),
                    due_at=parse_optional_datetime(item.get("due_at")),
                    starts_at=parse_optional_datetime(item.get("starts_at")),
                    ends_at=parse_optional_datetime(item.get("ends_at")),
                    source_type=str(item.get("source_type") or "raw_entry"),
                    source_id=int(item.get("source_id") or entry.id),
                    evidence=item.get("evidence") if isinstance(item.get("evidence"), list) else [{"raw_entry_id": entry.id}],
                    attributes=item.get("attributes") if isinstance(item.get("attributes"), dict) else {},
                )

            entry.processing_status = "processed"
            entry.extracted_at = datetime.now(timezone.utc)
        except Exception as exc:
            entry.processing_status = "error"
            entry.metadata_ = {**entry.metadata_, "processing_error": str(exc)}
    db.commit()
    return len(entries)


def add_memory(
    db: Session,
    *,
    kind: str,
    content: str,
    confidence: float = 0.5,
    attributes: dict | None = None,
    evidence: list | None = None,
) -> Memory:
    existing = (
        db.query(Memory)
        .filter(Memory.kind == kind, Memory.content == content, Memory.superseded_by_id.is_(None))
        .one_or_none()
    )
    if existing:
        existing.confidence = min(1.0, max(existing.confidence, confidence) + 0.05)
        existing.evidence = [*existing.evidence, *(evidence or [])]
        return existing
    memory = Memory(
        kind=kind,
        content=content,
        confidence=max(0.0, min(1.0, confidence)),
        attributes=attributes or {},
        evidence=evidence or [],
    )
    db.add(memory)
    db.flush()
    return memory


def ensure_persona(db: Session) -> PersonaProfile:
    persona = db.get(PersonaProfile, 1)
    if persona:
        return persona
    persona = PersonaProfile(id=1, timezone=settings.default_timezone, profile={"setup": "self-building"})
    db.add(persona)
    db.commit()
    db.refresh(persona)
    return persona


def get_or_create_chat_session(db: Session, session_id: int | None = None) -> ChatSession:
    now = datetime.now(timezone.utc)
    session = db.get(ChatSession, session_id) if session_id else None
    if session and is_session_active(session, now):
        return session
    session = db.query(ChatSession).order_by(desc(ChatSession.last_message_at), desc(ChatSession.updated_at)).first()
    if session and is_session_active(session, now):
        return session
    session = ChatSession(title=f"LifeOS chat {now.astimezone(local_tz()):%Y-%m-%d}", last_message_at=now)
    db.add(session)
    db.flush()
    return session


def add_chat_message(
    db: Session,
    *,
    session: ChatSession,
    role: str,
    content: str,
    sources: list | None = None,
    metadata: dict | None = None,
    analysis_status: str | None = None,
) -> ChatMessage:
    message_metadata = metadata or {}
    message = ChatMessage(
        session_id=session.id,
        role=role,
        content=content,
        sources=sources or [],
        metadata_=message_metadata,
        analysis_status=analysis_status or "pending",
        analysis_version=ANALYSIS_VERSION,
    )
    message.metadata_ = {**message.metadata_, "applicable_modes": applicable_analysis_modes(message), "analysis_modes": {}}
    session.last_message_at = datetime.now(timezone.utc)
    db.add(message)
    db.flush()
    return message


def record_chat_turn(db: Session, message: str, *, session_id: int | None = None) -> tuple[str, list[dict], int]:
    session = get_or_create_chat_session(db, session_id=session_id)
    user_message = add_chat_message(db, session=session, role="user", content=message, analysis_status="pending")
    created_items = []
    for item in extract_time_items_from_text(
        message,
        base_time=datetime.now(timezone.utc),
        source_type="chat_message",
        source_id=user_message.id,
    ):
        created_items.append(add_time_item(db, **item))
    answer, sources = answer_chat(db, message)
    if created_items:
        item_lines = "\n".join(f"- {item.kind}: {item.title}" for item in created_items)
        answer = f"Added to Daily Execution:\n{item_lines}\n\n{answer}"
        sources = [
            {"type": "time_item", "id": item.id, "kind": item.kind, "title": item.title}
            for item in created_items
        ] + sources
    add_chat_message(db, session=session, role="assistant", content=answer, sources=sources, analysis_status="pending")
    db.commit()
    refresh_dashboard_cards(db)
    return answer, sources, session.id


def time_item_to_card_item(item: TimeItem) -> dict[str, Any]:
    due_at = db_utc(item.due_at)
    starts_at = db_utc(item.starts_at)
    ends_at = db_utc(item.ends_at)
    return {
        "id": item.id,
        "time_item_id": item.id,
        "label": item.title,
        "kind": item.kind,
        "status": item.status,
        "priority": item.priority,
        "due_at": due_at.isoformat() if due_at else None,
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
    }


def build_execution_card(db: Session) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = start_of_day(now)
    today_end = end_of_day(now)
    open_items = (
        db.query(TimeItem)
        .filter(TimeItem.status.in_(("open", "snoozed")))
        .order_by(TimeItem.due_at.is_(None), TimeItem.due_at, desc(TimeItem.priority), TimeItem.created_at)
        .limit(40)
        .all()
    )
    overdue = [item for item in open_items if db_utc(item.due_at) and db_utc(item.due_at) < now]
    due_today = [item for item in open_items if db_utc(item.due_at) and today_start <= db_utc(item.due_at) < today_end]
    next_actions = [item for item in open_items if item.kind in {"task", "open_loop", "reminder"}][:6]
    scheduled = [item for item in open_items if item.kind in {"deadline", "event", "time_block"}][:6]

    if overdue:
        summary = f"{len(overdue)} overdue item{'s' if len(overdue) != 1 else ''}; clear these first."
    elif due_today:
        summary = f"{len(due_today)} item{'s' if len(due_today) != 1 else ''} due today."
    elif open_items:
        summary = "No urgent deadline found. Pick the next concrete action and protect focus time."
    else:
        summary = "No open tasks or deadlines yet. Add them in natural language."

    sections = [
        {"title": "Next Actions", "items": [time_item_to_card_item(item) for item in next_actions]},
        {"title": "Reminders & Deadlines", "items": [time_item_to_card_item(item) for item in scheduled]},
    ]
    return {
        "mode": "execution",
        "card_type": "daily_execution",
        "title": "Daily Execution",
        "priority": 95,
        "summary": summary,
        "metrics": {
            "open": len(open_items),
            "overdue": len(overdue),
            "due_today": len(due_today),
        },
        "sections": sections,
        "evidence": [{"object_type": "time_item", "object_id": item.id} for item in open_items[:12]],
    }


def build_analysis_card(db: Session) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    events = db.query(ExtractedEvent).filter(ExtractedEvent.occurred_at >= since).order_by(desc(ExtractedEvent.occurred_at)).all()
    analysis_messages = [
        message
        for message in db.query(ChatMessage).filter(ChatMessage.created_at >= since).order_by(desc(ChatMessage.created_at)).limit(40).all()
        if is_self_analysis_relevant(message.content)
    ][:8]
    counts = {event_type: sum(1 for event in events if event.event_type == event_type) for event_type in EVENT_TYPES}
    performance_events = [
        event
        for event in events
        if event.event_type in {"meal", "sleep", "mood", "symptom", "exercise", "work"}
    ][:8]
    if not events and not analysis_messages:
        summary = "No recent logs to analyze yet."
    elif counts["meal"] and (counts["mood"] or counts["symptom"]):
        summary = "Diet, energy, and symptom data are starting to form a useful pattern."
    else:
        summary = f"{len(events)} recent event{'s' if len(events) != 1 else ''} and {len(analysis_messages)} chat signal{'s' if len(analysis_messages) != 1 else ''} available for pattern analysis."
    return {
        "mode": "analysis",
        "card_type": "self_analysis",
        "title": "Self Analysis",
        "priority": 80,
        "summary": summary,
        "metrics": {
            "events_7d": len(events),
            "meals": counts["meal"],
            "mood_energy": counts["mood"],
            "symptoms": counts["symptom"],
            "chat_signals": len(analysis_messages),
        },
        "sections": [
            {
                "title": "Recent Signals",
                "items": [
                    {
                        "id": event.id,
                        "label": event.summary,
                        "kind": event.event_type,
                        "occurred_at": event.occurred_at.isoformat(),
                    }
                    for event in performance_events
                ]
                + [
                    {
                        "id": message.id,
                        "label": message.content,
                        "kind": "chat",
                        "occurred_at": message.created_at.isoformat(),
                    }
                    for message in analysis_messages
                ],
            }
        ],
        "evidence": [{"object_type": "event", "object_id": event.id} for event in performance_events]
        + [{"object_type": "chat_message", "object_id": message.id} for message in analysis_messages],
    }


def build_journal_card(db: Session) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = start_of_day(now)
    today_end = end_of_day(now)
    entries = (
        db.query(RawEntry)
        .filter(RawEntry.occurred_at >= today_start, RawEntry.occurred_at < today_end)
        .order_by(desc(RawEntry.occurred_at))
        .limit(8)
        .all()
    )
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.created_at >= today_start, ChatMessage.created_at < today_end)
        .order_by(desc(ChatMessage.created_at))
        .limit(8)
        .all()
    )
    summary = f"{len(entries)} log{'s' if len(entries) != 1 else ''} and {len(messages)} chat message{'s' if len(messages) != 1 else ''} today."
    return {
        "mode": "journal",
        "card_type": "life_journal",
        "title": "Life Journal",
        "priority": 70,
        "summary": summary,
        "metrics": {"logs_today": len(entries), "chat_messages_today": len(messages)},
        "sections": [
            {
                "title": "Today’s Logs",
                "items": [
                    {"id": entry.id, "label": entry.text, "kind": "log", "occurred_at": entry.occurred_at.isoformat()}
                    for entry in entries
                ],
            },
            {
                "title": "Chat History",
                "items": [
                    {"id": message.id, "label": message.content, "kind": message.role, "occurred_at": message.created_at.isoformat()}
                    for message in messages
                ],
            },
        ],
        "evidence": [{"object_type": "raw_entry", "object_id": entry.id} for entry in entries],
    }


def build_persona_card(db: Session) -> dict[str, Any]:
    persona = ensure_persona(db)
    memories = (
        db.query(Memory)
        .filter(Memory.superseded_by_id.is_(None))
        .order_by(desc(Memory.confidence), desc(Memory.updated_at))
        .limit(12)
        .all()
    )
    profile_items = [
        {"label": "Timezone", "value": persona.timezone},
        {"label": "Locale", "value": persona.locale},
    ]
    if persona.birth_year:
        profile_items.append({"label": "Birth year", "value": str(persona.birth_year)})
    if persona.gender:
        profile_items.append({"label": "Gender", "value": persona.gender})
    summary = f"{len(memories)} durable memor{'y' if len(memories) == 1 else 'ies'} with evidence-backed confidence."
    return {
        "mode": "persona",
        "card_type": "persona_memory",
        "title": "Persona",
        "priority": 60,
        "summary": summary,
        "metrics": {"memories": len(memories), "goals": len(persona.goals or [])},
        "sections": [
            {"title": "Stable Profile", "items": profile_items},
            {
                "title": "Memories",
                "items": [
                    {
                        "id": memory.id,
                        "label": memory.content,
                        "kind": memory.kind,
                        "confidence": memory.confidence,
                    }
                    for memory in memories
                ],
            },
        ],
        "evidence": [{"object_type": "memory", "object_id": memory.id} for memory in memories],
    }


def normalize_card(card: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(card, dict):
        return fallback
    mode = card.get("mode") if card.get("mode") in CARD_MODES else fallback["mode"]
    return {
        "mode": mode,
        "card_type": str(card.get("card_type") or fallback["card_type"])[:96],
        "title": str(card.get("title") or fallback["title"])[:160],
        "priority": max(0, min(100, int(card.get("priority") or fallback["priority"]))),
        "summary": str(card.get("summary") or fallback["summary"]),
        "metrics": card.get("metrics") if isinstance(card.get("metrics"), dict) else fallback.get("metrics", {}),
        "sections": card.get("sections") if isinstance(card.get("sections"), list) else fallback.get("sections", []),
        "evidence": card.get("evidence") if isinstance(card.get("evidence"), list) else fallback.get("evidence", []),
    }


def maybe_llm_card(db: Session, mode: str, fallback: dict[str, Any]) -> dict[str, Any]:
    context = {
        "mode": mode,
        "fallback_card": fallback,
        "recent_context": recent_context(db),
        "open_time_items": [
            time_item_to_card_item(item)
            for item in db.query(TimeItem).filter(TimeItem.status.in_(("open", "snoozed"))).order_by(TimeItem.due_at).limit(20).all()
        ],
    }
    prompt = (
        "You generate LifeOS dashboard cards as JSON only. Do not return HTML. "
        "Use this schema: mode, card_type, title, priority, summary, metrics, sections, evidence. "
        "Keep sections as an array of {title, items}; each item should be plain data with label/status/due_at when relevant. "
        f"Generate the {mode} card from this context:\n{context}"
    )
    try:
        parsed = safe_json_object(get_llm().chat([{"role": "user", "content": prompt}], temperature=0.1))
    except Exception:
        return fallback
    return normalize_card(parsed, fallback)


def build_card(db: Session, mode: str, *, use_llm: bool = False) -> dict[str, Any]:
    builders = {
        "execution": build_execution_card,
        "analysis": build_analysis_card,
        "journal": build_journal_card,
        "persona": build_persona_card,
    }
    fallback = builders[mode](db)
    return maybe_llm_card(db, mode, fallback) if use_llm else fallback


def persist_dashboard_card(
    db: Session,
    card: dict[str, Any],
    *,
    create_report: bool = False,
    report_date: date | None = None,
) -> DashboardCard:
    db.query(DashboardCard).filter(DashboardCard.mode == card["mode"], DashboardCard.status == "active").update(
        {"status": "archived"}
    )
    persisted = DashboardCard(
        mode=card["mode"],
        card_type=card["card_type"],
        title=card["title"],
        summary=card["summary"],
        priority=card["priority"],
        payload=card,
        evidence=card.get("evidence", []),
    )
    db.add(persisted)
    db.flush()
    if create_report:
        db.add(
            DailyReport(
                mode=card["mode"],
                report_date=report_date or datetime.now(local_tz()).date(),
                title=card["title"],
                body=card["summary"],
                payload=card,
                evidence=card.get("evidence", []),
            )
        )
    db.commit()
    db.refresh(persisted)
    return persisted


def refresh_dashboard_cards(
    db: Session,
    *,
    modes: list[str] | tuple[str, ...] | None = None,
    use_llm: bool = False,
    create_reports: bool = False,
) -> dict[str, DashboardCard]:
    selected_modes = modes or CARD_ORDER
    cards: dict[str, DashboardCard] = {}
    for mode in selected_modes:
        if mode not in CARD_MODES:
            continue
        cards[mode] = persist_dashboard_card(db, build_card(db, mode, use_llm=use_llm), create_report=create_reports)
    return cards


def ensure_dashboard_cards(db: Session) -> dict[str, DashboardCard]:
    cards: dict[str, DashboardCard] = {}
    missing = []
    for mode in CARD_ORDER:
        card = (
            db.query(DashboardCard)
            .filter(DashboardCard.mode == mode, DashboardCard.status == "active")
            .order_by(desc(DashboardCard.created_at))
            .first()
        )
        if card:
            cards[mode] = card
        else:
            missing.append(mode)
    if missing:
        cards.update(refresh_dashboard_cards(db, modes=missing))
    return cards


def card_to_dict(card: DashboardCard) -> dict[str, Any]:
    payload = card.payload or {}
    return {
        "id": card.id,
        "mode": card.mode,
        "card_type": card.card_type,
        "title": card.title,
        "summary": card.summary,
        "priority": card.priority,
        "metrics": payload.get("metrics", {}),
        "sections": payload.get("sections", []),
        "evidence": card.evidence,
        "created_at": card.created_at.isoformat(),
    }


def infer_persona_memories_from_message(db: Session, message: ChatMessage) -> list[Memory]:
    lower = message.content.lower()
    memories: list[Memory] = []
    if "self-esteem" in lower or "confidence" in lower:
        memories.append(
            add_memory(
                db,
                kind="self_belief",
                content=f"User reported: {message.content[:240]}",
                confidence=0.45,
                attributes={"source": "chat_analysis"},
                evidence=[{"chat_message_id": message.id}],
            )
        )
    if "i prefer" in lower or "i like" in lower or "i dislike" in lower:
        memories.append(
            add_memory(
                db,
                kind="preference",
                content=message.content[:500],
                confidence=0.5,
                attributes={"source": "chat_analysis"},
                evidence=[{"chat_message_id": message.id}],
            )
        )
    if "my goal" in lower or "my goals" in lower:
        memories.append(
            add_memory(
                db,
                kind="goal",
                content=message.content[:500],
                confidence=0.55,
                attributes={"source": "chat_analysis"},
                evidence=[{"chat_message_id": message.id}],
            )
        )
    return memories


def next_hourly_analysis_mode(db: Session) -> str:
    last = (
        db.query(AgentRun)
        .filter(AgentRun.job_name == "hourly_analysis", AgentRun.status == "success", AgentRun.mode.in_(ANALYSIS_MODES))
        .order_by(desc(AgentRun.finished_at), desc(AgentRun.started_at))
        .first()
    )
    if not last or last.mode not in ANALYSIS_MODES:
        return ANALYSIS_MODES[0]
    return ANALYSIS_MODES[(ANALYSIS_MODES.index(last.mode) + 1) % len(ANALYSIS_MODES)]


def analysis_card_modes(mode: str) -> tuple[str, ...]:
    return {
        "daily_execution": ("execution",),
        "self_analysis": ("analysis",),
        "life_journal": ("journal",),
        "persona_refresh": ("persona",),
    }.get(mode, ())


def run_mode_analysis(db: Session, mode: str) -> tuple[list[int], list[int]]:
    messages = pending_messages_for_mode(db, mode)
    now = datetime.now(timezone.utc)
    for message in messages:
        try:
            if mode == "daily_execution" and message.role == "user":
                existing = (
                    db.query(TimeItem)
                    .filter(TimeItem.source_type == "chat_message", TimeItem.source_id == message.id)
                    .first()
                )
                if not existing:
                    for item in extract_time_items_from_text(
                        message.content,
                        base_time=db_utc(message.created_at) or now,
                        source_type="chat_message",
                        source_id=message.id,
                    ):
                        add_time_item(db, **item)
            if mode == "persona_refresh" and message.role == "user":
                for memory in infer_persona_memories_from_message(db, message):
                    upsert_embedding(db, "memory", memory.id, memory.content)
            set_message_analysis_coverage(message, mode, analyzed_at=now)
        except Exception as exc:
            set_message_analysis_coverage(message, mode, error=str(exc))
    db.commit()

    created_cards = refresh_dashboard_cards(
        db,
        modes=analysis_card_modes(mode),
        use_llm=True,
        create_reports=True,
    )
    return [message.id for message in messages], [card.id for card in created_cards.values()]


def reflect_recent_data(db: Session) -> Recommendation | None:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    events = db.query(ExtractedEvent).filter(ExtractedEvent.occurred_at >= since).order_by(desc(ExtractedEvent.occurred_at)).all()
    if not events:
        return None
    meal_count = sum(1 for event in events if event.event_type == "meal")
    symptom_count = sum(1 for event in events if event.event_type == "symptom")
    mood_count = sum(1 for event in events if event.event_type == "mood")
    title = "Recent pattern to watch"
    if meal_count and symptom_count:
        body = "You logged meals and possible energy or symptom changes recently. Keep noting meal timing, caffeine, energy, focus, and crashes so LifeOS can find stronger diet-performance patterns."
    elif mood_count:
        body = "You have recent mood or energy logs. Add context about sleep, caffeine, meals, and work blocks to make future advice more specific."
    else:
        body = "You have new logs. Keep using natural language; LifeOS will preserve raw entries and extract more structure over time."
    recommendation = Recommendation(
        title=title,
        body=body,
        evidence=[{"event_ids": [event.id for event in events[:12]]}],
    )
    db.add(recommendation)
    db.commit()
    db.refresh(recommendation)
    return recommendation


def run_job(db: Session, job_name: str) -> AgentRun:
    mode = next_hourly_analysis_mode(db) if job_name == "hourly_analysis" else job_name if job_name in ANALYSIS_MODES else None
    run = AgentRun(
        job_name=job_name,
        mode=mode,
        model=settings.ollama_model,
        prompt_version=PROMPT_VERSION,
        status="running",
        input_message_ids=[],
        output_card_ids=[],
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    try:
        if job_name == "ingest":
            count = process_pending_entries(db)
            refresh_dashboard_cards(db, modes=("execution", "analysis", "journal", "persona"))
            run.summary = f"Processed {count} pending entries and refreshed dashboard cards."
        elif job_name == "hourly_analysis":
            input_ids, card_ids = run_mode_analysis(db, mode or "daily_execution")
            run.input_message_ids = input_ids
            run.output_card_ids = card_ids
            run.summary = f"Ran {mode} analysis over {len(input_ids)} message(s)."
        elif job_name in ANALYSIS_MODES:
            if job_name == "self_analysis":
                reflect_recent_data(db)
            if job_name == "persona_refresh":
                ensure_persona(db)
            input_ids, card_ids = run_mode_analysis(db, job_name)
            run.input_message_ids = input_ids
            run.output_card_ids = card_ids
            run.summary = f"Ran {job_name} analysis over {len(input_ids)} message(s)."
        elif job_name in {"reflect", "nightly"}:
            reflect_recent_data(db)
            all_input_ids: list[int] = []
            all_card_ids: list[int] = []
            for selected_mode in ANALYSIS_MODES:
                input_ids, card_ids = run_mode_analysis(db, selected_mode)
                all_input_ids.extend(input_ids)
                all_card_ids.extend(card_ids)
            run.input_message_ids = sorted(set(all_input_ids))
            run.output_card_ids = all_card_ids
            run.summary = "Generated execution, analysis, journal, and persona cards."
        else:
            run.summary = "No-op job."
        run.status = "success"
    except Exception as exc:
        run.status = "error"
        run.error = str(exc)
    finally:
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)
    return run


def time_items_between(db: Session, start: datetime, end: datetime) -> list[TimeItem]:
    return (
        db.query(TimeItem)
        .filter(
            or_(
                (TimeItem.due_at >= start) & (TimeItem.due_at < end),
                (TimeItem.starts_at >= start) & (TimeItem.starts_at < end),
            )
        )
        .order_by(TimeItem.due_at, TimeItem.starts_at)
        .all()
    )


def answer_chat(db: Session, message: str) -> tuple[str, list[dict]]:
    date_range = parse_date_range(message)
    sources: list[dict] = []
    if date_range:
        events = events_between(db, *date_range)
        time_items = time_items_between(db, *date_range)
        sources = [
            {
                "type": "event",
                "id": event.id,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "summary": event.summary,
            }
            for event in events
        ] + [
            {
                "type": "time_item",
                "id": item.id,
                "kind": item.kind,
                "due_at": item.due_at.isoformat() if item.due_at else None,
                "title": item.title,
            }
            for item in time_items
        ]
        if not events and not time_items:
            return "I could not find any logged events, tasks, reminders, or deadlines for that date.", sources
        lines = [f"{event.occurred_at:%H:%M} [{event.event_type}] {event.summary}" for event in events]
        lines += [
            f"{(item.due_at or item.starts_at):%H:%M} [{item.kind}] {item.title}"
            for item in time_items
            if item.due_at or item.starts_at
        ]
        return "Here is what I found from your database:\n" + "\n".join(lines), sources

    semantic = semantic_search(db, message, limit=6)
    context = recent_context(db)
    context["open_time_items"] = [
        time_item_to_card_item(item)
        for item in db.query(TimeItem).filter(TimeItem.status.in_(("open", "snoozed"))).order_by(TimeItem.due_at).limit(20).all()
    ]
    sources = semantic
    system = (
        "You are LifeOS, a local personal assistant. Answer using the supplied database context. "
        "If a factual question requires dates or exact history and the context is insufficient, say what data is missing. "
        "Be practical and concise. Avoid medical diagnosis; frame health advice as patterns to test."
    )
    payload = {"semantic_matches": semantic, "recent_context": context}
    try:
        answer = get_llm().chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Database context:\n{payload}\n\nQuestion:\n{message}"},
            ],
            temperature=0.2,
        )
    except Exception:
        if semantic:
            bullets = "\n".join(f"- {item['content']}" for item in semantic[:4])
            answer = f"I found related local context, but Ollama is not reachable yet:\n{bullets}"
        else:
            answer = "I do not have enough local context yet, and Ollama is not reachable. Add logs or start Ollama, then ask again."
    return answer, sources

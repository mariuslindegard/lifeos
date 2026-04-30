import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, or_, text
from sqlalchemy.orm import Session

from lifeos.config import settings
from lifeos.llm import get_llm, safe_json_object
from lifeos.models import (
    AgentRun,
    ChatMessage,
    ChatSession,
    DashboardCard,
    DailyReport,
    ExtractedEvent,
    Memory,
    PersonaProfile,
    RawEntry,
    ReflectionSummary,
    TimeItem,
)
from lifeos.rag import (
    HistoricalContext,
    ResolvedTimeWindow,
    historical_context,
    parse_comparison_time_windows,
    parse_time_window,
    recent_context,
    semantic_search,
    upsert_embedding,
)

EVENT_TYPES = ("meal", "exercise", "sleep", "mood", "symptom", "work", "activity", "note")
TIME_ITEM_TYPES = ("task", "reminder", "deadline", "event", "time_block", "open_loop")
CARD_MODES = ("overview", "execution", "analysis", "journal", "persona")
CARD_ORDER = ("overview",)
ANALYSIS_VERSION = "v1"
PROMPT_VERSION = "v3"

REFLECTION_PERIODS = (
    ("yesterday", "Previous day", 1),
    ("past_7_days", "Past 7 days", 7),
    ("past_30_days", "Past 30 days", 30),
    ("past_6_months", "Past 6 months", 183),
    ("past_1_year", "Past 1 year", 365),
)
REFLECTION_LABELS = {key: label for key, label, _days in REFLECTION_PERIODS}

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


def local_tz(timezone_name: str | None = None) -> ZoneInfo:
    return ZoneInfo(timezone_name or settings.default_timezone)


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


def local_day_bounds(day_value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day_value, time.min, tzinfo=local_tz())
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def latest_completed_local_day(now: datetime | None = None) -> date:
    now = db_utc(now) or datetime.now(timezone.utc)
    return now.astimezone(local_tz()).date() - timedelta(days=1)


def reflection_window(anchor_date: date, days: int) -> tuple[datetime, datetime]:
    start_date = anchor_date - timedelta(days=days - 1)
    start, _mid = local_day_bounds(start_date)
    _start_anchor, end = local_day_bounds(anchor_date)
    return start, end


def is_session_active(session: ChatSession, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    last_message_at = db_utc(session.last_message_at or session.created_at)
    if not last_message_at:
        return True
    if now - last_message_at >= timedelta(hours=6):
        return False
    return local_date(last_message_at) == local_date(now)


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


def is_analytical_history_question(text_value: str) -> bool:
    lower = text_value.lower()
    return any(
        phrase in lower
        for phrase in (
            "why",
            "pattern",
            "patterns",
            "analy",
            "energy",
            "mood",
            "symptom",
            "trend",
            "changed",
            "change between",
            "compare",
            "how was",
            "how were",
        )
    )


def is_temporal_query_intent(text_value: str) -> bool:
    lower = text_value.lower().strip()
    return (
        "?" in text_value
        or lower.startswith(("what", "when", "how", "did", "was", "were", "compare", "summarize", "show", "tell me", "review", "analyze", "give me"))
        or "what changed" in lower
        or "between " in lower
    )


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
    title = re.sub(
        r"^\s*(remind me to|remind me|remember to|deadline for|deadline|todo:?|task:?|i need to|need to)\s+",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(
        r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\b(?:at\s*)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", "", title, flags=re.I)
    title = re.sub(
        r"\b(?:by|on|for)\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*20\d{2})?\b",
        "",
        title,
        flags=re.I,
    )
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
        f"event_type must be one of: {', '.join(EVENT_TYPES)}. "
        "memories should contain kind, content, confidence, attributes. "
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
    if is_persona_relevant(entry.text):
        for memory in infer_persona_memories_from_text(db, entry.text, evidence=[{"raw_entry_id": entry.id}]):
            upsert_embedding(db, "memory", memory.id, memory.content)
    db.commit()
    db.refresh(entry)
    process_pending_entries(db, limit=1)
    refresh_overview_card(db)
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

            for item in extracted.get("time_items") or []:
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


def infer_persona_memories_from_text(db: Session, text_value: str, *, evidence: list[dict[str, Any]]) -> list[Memory]:
    lower = text_value.lower()
    created: list[Memory] = []
    if "i prefer" in lower or "i like" in lower or "i dislike" in lower:
        created.append(
            add_memory(
                db,
                kind="preference",
                content=text_value[:500],
                confidence=0.55,
                attributes={"source": "direct_ingestion"},
                evidence=evidence,
            )
        )
    if "my goal" in lower or "my goals" in lower:
        created.append(
            add_memory(
                db,
                kind="goal",
                content=text_value[:500],
                confidence=0.6,
                attributes={"source": "direct_ingestion"},
                evidence=evidence,
            )
        )
    if "i am" in lower or "i'm" in lower or "i tend" in lower or "i usually" in lower:
        created.append(
            add_memory(
                db,
                kind="trait",
                content=text_value[:500],
                confidence=0.5,
                attributes={"source": "direct_ingestion"},
                evidence=evidence,
            )
        )
    if any(word in lower for word in ("stress", "anxious", "tired", "energized", "self-esteem", "confidence")):
        created.append(
            add_memory(
                db,
                kind="wellbeing_signal",
                content=text_value[:500],
                confidence=0.5,
                attributes={"source": "direct_ingestion"},
                evidence=evidence,
            )
        )
    if any(word in lower for word in ("deep work", "focus block", "work best", "selling", "meeting")):
        created.append(
            add_memory(
                db,
                kind="work_style",
                content=text_value[:500],
                confidence=0.45,
                attributes={"source": "direct_ingestion"},
                evidence=evidence,
            )
        )
    return created


def default_persona_profile() -> dict[str, Any]:
    return {
        "name": "",
        "life_stage": "",
        "personality_summary": "",
        "wellbeing_baseline": "",
        "focus_areas": [],
        "values": [],
        "preferences": [],
        "constraints": [],
        "goals": [],
        "setup": "self-building",
    }


def persona_summary_line(items: list[dict[str, Any]], empty_value: str, *, limit: int = 3) -> str:
    picked: list[str] = []
    for item in items[:limit]:
        content = str(item.get("content") or "").strip()
        if content and content not in picked:
            picked.append(content)
    return " ".join(picked) if picked else empty_value


def ensure_persona(db: Session) -> PersonaProfile:
    persona = db.get(PersonaProfile, 1)
    if persona:
        persona.profile = {**default_persona_profile(), **(persona.profile or {})}
        return persona
    persona = PersonaProfile(id=1, timezone=settings.default_timezone, profile=default_persona_profile())
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
    message = ChatMessage(
        session_id=session.id,
        role=role,
        content=content,
        sources=sources or [],
        metadata_=metadata or {},
        analysis_status=analysis_status or "complete",
        analysis_version=ANALYSIS_VERSION,
    )
    message.analyzed_at = datetime.now(timezone.utc)
    session.last_message_at = datetime.now(timezone.utc)
    db.add(message)
    db.flush()
    return message


def prepare_chat_turn(
    db: Session,
    message: str,
    *,
    session_id: int | None = None,
) -> tuple[ChatSession, ChatMessage, list[TimeItem]]:
    session = get_or_create_chat_session(db, session_id=session_id)
    user_message = add_chat_message(db, session=session, role="user", content=message)
    if is_persona_relevant(message):
        for memory in infer_persona_memories_from_text(db, message, evidence=[{"chat_message_id": user_message.id}]):
            upsert_embedding(db, "memory", memory.id, memory.content)
    created_items = []
    for item in extract_time_items_from_text(
        message,
        base_time=datetime.now(timezone.utc),
        source_type="chat_message",
        source_id=user_message.id,
    ):
        created_items.append(add_time_item(db, **item))
    db.commit()
    db.refresh(session)
    db.refresh(user_message)
    return session, user_message, created_items


def created_item_prefix(created_items: list[TimeItem]) -> tuple[str, list[dict[str, Any]]]:
    if not created_items:
        return "", []
    item_lines = "\n".join(f"- {item.kind}: {item.title}" for item in created_items)
    prefix = f"Added to Daily Execution:\n{item_lines}\n\n"
    sources = [{"type": "time_item", "id": item.id, "kind": item.kind, "title": item.title} for item in created_items]
    return prefix, sources


def persist_assistant_turn(db: Session, session: ChatSession, answer: str, sources: list[dict[str, Any]]) -> None:
    add_chat_message(db, session=session, role="assistant", content=answer, sources=sources)
    db.commit()
    refresh_overview_card(db)


def record_chat_turn(db: Session, message: str, *, session_id: int | None = None) -> tuple[str, list[dict], int]:
    session, _user_message, created_items = prepare_chat_turn(db, message, session_id=session_id)
    answer, sources = answer_chat(db, message)
    prefix, prefix_sources = created_item_prefix(created_items)
    answer = prefix + answer
    sources = prefix_sources + sources
    persist_assistant_turn(db, session, answer, sources)
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


def normalize_string_list(value: Any, *, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text_value = str(item).strip()
        if text_value and text_value not in items:
            items.append(text_value[:240])
        if len(items) >= limit:
            break
    return items


def summarize_recent_items(items: list[str], empty_value: str) -> list[str]:
    return items[:4] if items else [empty_value]


def window_signal_data(db: Session, start: datetime, end: datetime) -> dict[str, Any]:
    entries = (
        db.query(RawEntry)
        .filter(RawEntry.occurred_at >= start, RawEntry.occurred_at < end)
        .order_by(desc(RawEntry.occurred_at))
        .limit(60)
        .all()
    )
    events = (
        db.query(ExtractedEvent)
        .filter(ExtractedEvent.occurred_at >= start, ExtractedEvent.occurred_at < end)
        .order_by(desc(ExtractedEvent.occurred_at))
        .limit(80)
        .all()
    )
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.created_at >= start, ChatMessage.created_at < end)
        .order_by(desc(ChatMessage.created_at))
        .limit(60)
        .all()
    )
    time_items = time_items_between(db, start, end)
    open_loops = (
        db.query(TimeItem)
        .filter(TimeItem.status.in_(("open", "snoozed")))
        .order_by(TimeItem.due_at.is_(None), TimeItem.due_at, desc(TimeItem.priority), TimeItem.created_at)
        .limit(8)
        .all()
    )
    return {
        "entries": entries,
        "events": events,
        "messages": messages,
        "time_items": time_items,
        "open_loops": open_loops,
    }


def reflection_evidence_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for event in data["events"][:6]:
        evidence.append({"object_type": "event", "object_id": event.id})
    for message in data["messages"][:3]:
        evidence.append({"object_type": "chat_message", "object_id": message.id})
    for item in data["time_items"][:3]:
        evidence.append({"object_type": "time_item", "object_id": item.id})
    return evidence


def reflection_metrics_payload(data: dict[str, Any]) -> dict[str, int]:
    counts = {event_type: 0 for event_type in EVENT_TYPES}
    for event in data["events"]:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return {
        "logs": len(data["entries"]),
        "events": len(data["events"]),
        "chat_signals": len(data["messages"]),
        "open_loops": len(data["open_loops"]),
        "meals": counts["meal"],
        "mood_signals": counts["mood"],
        "symptoms": counts["symptom"],
        "work_signals": counts["work"],
    }


def reflection_fallback_summary(
    period_key: str,
    anchor_date: date,
    start: datetime,
    end: datetime,
    data: dict[str, Any],
    smaller_summaries: list[ReflectionSummary],
) -> dict[str, Any]:
    metrics = reflection_metrics_payload(data)
    event_lines = [event.summary for event in data["events"][:4]]
    message_lines = [message.content for message in data["messages"][:3] if message.role == "user"]
    open_loop_titles = [item.title for item in data["open_loops"][:5]]
    carry_forward_seed: list[str] = []
    for summary in smaller_summaries:
        payload = summary.payload or {}
        carry_forward_seed.extend(normalize_string_list(payload.get("carry_forward_points"), limit=4))
        headline = str(payload.get("headline") or "").strip()
        if headline:
            carry_forward_seed.append(headline)
    carry_forward_points = []
    for item in carry_forward_seed:
        if item not in carry_forward_points:
            carry_forward_points.append(item)
        if len(carry_forward_points) >= 4:
            break
    if not carry_forward_points and event_lines:
        carry_forward_points = [event_lines[0]]

    wins: list[str] = []
    if metrics["logs"]:
        wins.append(f"Captured {metrics['logs']} log entries in this window.")
    if metrics["work_signals"]:
        wins.append(f"Recorded {metrics['work_signals']} work-related signals worth preserving.")
    if metrics["meals"] and (metrics["mood_signals"] or metrics["symptoms"]):
        wins.append("Food, energy, or symptom data is dense enough to support pattern tracking.")
    wins = summarize_recent_items(wins, "Keep logging concrete moments so the reflection becomes more precise.")

    risks: list[str] = []
    if open_loop_titles:
        risks.append(f"Open loops still active: {', '.join(open_loop_titles[:3])}.")
    if metrics["symptoms"]:
        risks.append("Symptoms or crashes were mentioned and should stay visible in the next cycle.")
    if not metrics["logs"]:
        risks.append("This window is sparse, so confidence is limited.")
    risks = summarize_recent_items(risks, "No urgent risks surfaced from the available data.")

    patterns: list[str] = []
    if event_lines:
        patterns.append(f"Recent signal: {event_lines[0]}")
    if len(event_lines) > 1:
        patterns.append(f"Related signal: {event_lines[1]}")
    if message_lines:
        patterns.append(f"Chat context: {message_lines[0][:180]}")
    if carry_forward_points:
        patterns.append(f"Carry forward: {carry_forward_points[0]}")
    patterns = summarize_recent_items(patterns, "No strong recurring pattern is visible yet.")

    narrative_parts = [
        f"{metrics['logs']} logs, {metrics['events']} structured events, and {metrics['chat_signals']} chat signals were captured.",
    ]
    if open_loop_titles:
        narrative_parts.append(f"Priority open loops: {', '.join(open_loop_titles[:3])}.")
    if carry_forward_points:
        narrative_parts.append(f"Important context to preserve: {carry_forward_points[0]}.")
    narrative = " ".join(narrative_parts)

    headline = (
        f"{REFLECTION_LABELS[period_key]} reflection for {anchor_date.isoformat()}: "
        f"{metrics['logs']} logs, {metrics['events']} events, {len(open_loop_titles)} open loops."
    )
    body = f"{narrative} Next focus: {risks[0]}"
    return {
        "period_key": period_key,
        "label": REFLECTION_LABELS[period_key],
        "anchor_date": anchor_date.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "title": REFLECTION_LABELS[period_key],
        "body": body,
        "headline": headline,
        "narrative": narrative,
        "wins": wins,
        "risks": risks,
        "patterns": patterns,
        "carry_forward_points": carry_forward_points or wins[:1],
        "open_loops": open_loop_titles,
        "metrics": metrics,
        "evidence": reflection_evidence_payload(data),
    }


def maybe_llm_reflection_summary(
    period_key: str,
    anchor_date: date,
    start: datetime,
    end: datetime,
    data: dict[str, Any],
    smaller_summaries: list[ReflectionSummary],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    context = {
        "period_key": period_key,
        "anchor_date": anchor_date.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "events": [
            {"type": event.event_type, "occurred_at": event.occurred_at.isoformat(), "summary": event.summary}
            for event in data["events"][:18]
        ],
        "messages": [
            {"role": message.role, "created_at": message.created_at.isoformat(), "content": message.content[:220]}
            for message in data["messages"][:12]
        ],
        "time_items": [time_item_to_card_item(item) for item in data["time_items"][:12]],
        "smaller_summaries": [
            {
                "period_key": summary.period_key,
                "title": summary.title,
                "body": summary.body,
                "carry_forward_points": normalize_string_list((summary.payload or {}).get("carry_forward_points")),
            }
            for summary in smaller_summaries
        ],
        "fallback": fallback,
    }
    prompt = (
        "Generate a LifeOS reflection summary as JSON only. "
        "Return keys: title, body, headline, narrative, wins, risks, patterns, carry_forward_points, open_loops, metrics. "
        "Keep wins/risks/patterns/carry_forward_points/open_loops as short arrays of strings. "
        "Preserve important context from smaller summaries when relevant.\n\n"
        f"{context}"
    )
    try:
        parsed = safe_json_object(get_llm().chat([{"role": "user", "content": prompt}], temperature=0.1))
    except Exception:
        return fallback
    if not parsed:
        return fallback
    merged = dict(fallback)
    merged.update(
        {
            "title": str(parsed.get("title") or fallback["title"])[:160],
            "body": str(parsed.get("body") or fallback["body"]),
            "headline": str(parsed.get("headline") or fallback["headline"]),
            "narrative": str(parsed.get("narrative") or fallback["narrative"]),
            "wins": normalize_string_list(parsed.get("wins")) or fallback["wins"],
            "risks": normalize_string_list(parsed.get("risks")) or fallback["risks"],
            "patterns": normalize_string_list(parsed.get("patterns")) or fallback["patterns"],
            "carry_forward_points": normalize_string_list(parsed.get("carry_forward_points")) or fallback["carry_forward_points"],
            "open_loops": normalize_string_list(parsed.get("open_loops")) or fallback["open_loops"],
        }
    )
    metrics = parsed.get("metrics")
    merged["metrics"] = metrics if isinstance(metrics, dict) else fallback["metrics"]
    return merged


def upsert_reflection_summary(
    db: Session,
    *,
    period_key: str,
    anchor_date: date,
    start: datetime,
    end: datetime,
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
    source_summary_ids: list[int],
) -> ReflectionSummary:
    summary = (
        db.query(ReflectionSummary)
        .filter(ReflectionSummary.period_key == period_key, ReflectionSummary.anchor_date == anchor_date)
        .one_or_none()
    )
    if summary is None:
        summary = ReflectionSummary(
            period_key=period_key,
            anchor_date=anchor_date,
            window_start=start,
            window_end=end,
            title=str(payload["title"])[:160],
            body=str(payload["body"]),
            payload=payload,
            evidence=evidence,
            source_summary_ids=source_summary_ids,
        )
        db.add(summary)
        db.flush()
        return summary
    summary.window_start = start
    summary.window_end = end
    summary.title = str(payload["title"])[:160]
    summary.body = str(payload["body"])
    summary.payload = payload
    summary.evidence = evidence
    summary.source_summary_ids = source_summary_ids
    db.flush()
    return summary


def generate_reflection_summaries(
    db: Session,
    *,
    anchor_date: date | None = None,
    use_llm: bool = True,
) -> list[ReflectionSummary]:
    anchor_date = anchor_date or latest_completed_local_day()
    generated: list[ReflectionSummary] = []
    for period_key, _label, days in REFLECTION_PERIODS:
        start, end = reflection_window(anchor_date, days)
        data = window_signal_data(db, start, end)
        fallback = reflection_fallback_summary(period_key, anchor_date, start, end, data, generated)
        payload = maybe_llm_reflection_summary(period_key, anchor_date, start, end, data, generated, fallback) if use_llm else fallback
        summary = upsert_reflection_summary(
            db,
            period_key=period_key,
            anchor_date=anchor_date,
            start=start,
            end=end,
            payload=payload,
            evidence=reflection_evidence_payload(data),
            source_summary_ids=[item.id for item in generated],
        )
        generated.append(summary)
    db.commit()
    return generated


def ensure_reflection_summaries(db: Session, *, anchor_date: date | None = None) -> list[ReflectionSummary]:
    anchor_date = anchor_date or latest_completed_local_day()
    existing = (
        db.query(ReflectionSummary)
        .filter(ReflectionSummary.anchor_date == anchor_date)
        .order_by(ReflectionSummary.created_at)
        .all()
    )
    keyed = {summary.period_key: summary for summary in existing}
    if all(key in keyed for key, _label, _days in REFLECTION_PERIODS):
        return [keyed[key] for key, _label, _days in REFLECTION_PERIODS]
    return generate_reflection_summaries(db, anchor_date=anchor_date, use_llm=False)


def serialize_reflection_summary(summary: ReflectionSummary) -> dict[str, Any]:
    payload = summary.payload or {}
    return {
        "id": summary.id,
        "period_key": summary.period_key,
        "label": payload.get("label") or REFLECTION_LABELS.get(summary.period_key, summary.period_key),
        "anchor_date": summary.anchor_date.isoformat(),
        "window_start": summary.window_start.isoformat(),
        "window_end": summary.window_end.isoformat(),
        "title": summary.title,
        "body": summary.body,
        "headline": payload.get("headline", summary.title),
        "narrative": payload.get("narrative", summary.body),
        "wins": payload.get("wins", []),
        "risks": payload.get("risks", []),
        "patterns": payload.get("patterns", []),
        "carry_forward_points": payload.get("carry_forward_points", []),
        "open_loops": payload.get("open_loops", []),
        "metrics": payload.get("metrics", {}),
        "evidence": summary.evidence,
        "source_summary_ids": summary.source_summary_ids,
        "created_at": summary.created_at.isoformat(),
    }


def current_day_signal_data(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    now = db_utc(now) or datetime.now(timezone.utc)
    start = start_of_day(now)
    end = end_of_day(now)
    recent_start = now - timedelta(days=3)
    entries = (
        db.query(RawEntry)
        .filter(RawEntry.occurred_at >= start, RawEntry.occurred_at < end)
        .order_by(desc(RawEntry.occurred_at))
        .limit(24)
        .all()
    )
    events = (
        db.query(ExtractedEvent)
        .filter(ExtractedEvent.occurred_at >= start, ExtractedEvent.occurred_at < end)
        .order_by(desc(ExtractedEvent.occurred_at))
        .limit(30)
        .all()
    )
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.created_at >= start, ChatMessage.created_at < end)
        .order_by(desc(ChatMessage.created_at))
        .limit(20)
        .all()
    )
    recent_messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.created_at >= recent_start, ChatMessage.created_at < now, ChatMessage.role == "user")
        .order_by(desc(ChatMessage.created_at))
        .limit(12)
        .all()
    )
    upcoming_items = (
        db.query(TimeItem)
        .filter(TimeItem.status.in_(("open", "snoozed")))
        .filter(
            or_(
                (TimeItem.due_at >= now) & (TimeItem.due_at < end),
                (TimeItem.starts_at >= now) & (TimeItem.starts_at < end),
            )
        )
        .order_by(TimeItem.starts_at.is_(None), TimeItem.starts_at, TimeItem.due_at.is_(None), TimeItem.due_at, desc(TimeItem.priority))
        .limit(8)
        .all()
    )
    urgent_items = urgent_items_for_overview(db)
    return {
        "now": now,
        "start": start,
        "end": end,
        "entries": entries,
        "events": events,
        "messages": messages,
        "recent_messages": recent_messages,
        "upcoming_items": upcoming_items,
        "urgent_items": urgent_items,
    }


def current_day_evidence_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for entry in data["entries"][:3]:
        evidence.append({"object_type": "raw_entry", "object_id": entry.id})
    for event in data["events"][:4]:
        evidence.append({"object_type": "event", "object_id": event.id})
    for message in data["messages"][:3]:
        evidence.append({"object_type": "chat_message", "object_id": message.id})
    for item in data["upcoming_items"][:3]:
        evidence.append({"object_type": "time_item", "object_id": item.id})
    return evidence


def build_current_day_brief(db: Session, summaries: list[ReflectionSummary], *, now: datetime | None = None) -> dict[str, Any]:
    data = current_day_signal_data(db, now=now)
    now = data["now"]
    current_day = now.astimezone(local_tz()).date().isoformat()
    entry_lines = [entry.text.strip()[:180] for entry in data["entries"][:3] if entry.text.strip()]
    event_lines = [event.summary.strip()[:180] for event in data["events"][:4] if event.summary.strip()]
    message_lines = [message.content.strip()[:180] for message in data["messages"][:4] if message.role == "user" and message.content.strip()]
    recent_lines = [message.content.strip()[:180] for message in data["recent_messages"] if message.content.strip()]
    upcoming_titles = [item.title for item in data["upcoming_items"][:4]]
    urgent_card_items = [time_item_to_card_item(item) for item in data["urgent_items"]]

    recent_relevant_signals: list[str] = []
    for line in event_lines[:2]:
        recent_relevant_signals.append(line)
    for line in message_lines[:2]:
        if line not in recent_relevant_signals:
            recent_relevant_signals.append(line)
    for line in recent_lines[:2]:
        lower = line.lower()
        if any(term in lower for term in ("today", "tomorrow", "later", "meeting", "remind", "need to", "plan", "focus")) and line not in recent_relevant_signals:
            recent_relevant_signals.append(line)
    recent_relevant_signals = recent_relevant_signals[:4]

    today_focus: list[str] = []
    if upcoming_titles:
        today_focus.append(f"Upcoming today: {', '.join(upcoming_titles[:3])}.")
    if event_lines:
        today_focus.append(f"Already captured: {event_lines[0]}")
    if message_lines:
        today_focus.append(f"Chat focus: {message_lines[0]}")
    if not today_focus and summaries:
        carry = normalize_string_list((summaries[0].payload or {}).get("carry_forward_points"))
        if carry:
            today_focus.append(f"Carry-over to watch: {carry[0]}")

    tips: list[str] = []
    if upcoming_titles:
        tips.append("Use the next event or reminder as the anchor for your next block.")
    if data["urgent_items"]:
        tips.append("Close one open loop early so the rest of the day has less drag.")
    if recent_relevant_signals:
        tips.append("Keep feeding short concrete logs so the brief can sharpen during the day.")
    if not tips:
        tips.append("Begynn å chatte for å gi meg noe å analysere!")

    signal_count = len(data["entries"]) + len(data["events"]) + len(message_lines) + len(upcoming_titles) + len(recent_relevant_signals)
    if signal_count == 0:
        message = "Begynn å chatte for å gi meg noe å analysere!"
        confidence = "empty"
        today_focus = []
        recent_relevant_signals = []
        tips = []
    else:
        message_parts: list[str] = []
        if upcoming_titles:
            message_parts.append(f"{len(upcoming_titles)} upcoming item{'s' if len(upcoming_titles) != 1 else ''} on deck.")
        if event_lines:
            message_parts.append(f"Latest signal: {event_lines[0]}")
        elif entry_lines:
            message_parts.append(f"Latest log: {entry_lines[0]}")
        if message_lines:
            message_parts.append(f"Today's chat context: {message_lines[0]}")
        elif recent_relevant_signals:
            message_parts.append(f"Recent context still relevant: {recent_relevant_signals[0]}")
        message = " ".join(message_parts[:3]) or "Begynn å chatte for å gi meg noe å analysere!"
        confidence = "high" if signal_count >= 6 else "medium" if signal_count >= 3 else "low"

    return {
        "headline": f"Dagens brief for {current_day}",
        "message": message,
        "today_focus": today_focus[:4],
        "upcoming_items": [time_item_to_card_item(item) for item in data["upcoming_items"]],
        "recent_relevant_signals": recent_relevant_signals,
        "tips": tips[:3],
        "confidence": confidence,
        "metrics": {
            "today_logs": len(data["entries"]),
            "today_events": len(data["events"]),
            "today_messages": len(message_lines),
            "upcoming_items": len(data["upcoming_items"]),
            "urgent_items": len(urgent_card_items),
        },
        "evidence": current_day_evidence_payload(data),
        "generated_for_day": current_day,
        "generated_at": now.isoformat(),
    }


def urgent_items_for_overview(db: Session) -> list[TimeItem]:
    now = datetime.now(timezone.utc)
    today_start = start_of_day(now)
    today_end = end_of_day(now)
    urgent = (
        db.query(TimeItem)
        .filter(TimeItem.status.in_(("open", "snoozed")))
        .filter(
            or_(
                TimeItem.due_at < now,
                (TimeItem.due_at >= today_start) & (TimeItem.due_at < today_end),
                (TimeItem.starts_at >= today_start) & (TimeItem.starts_at < today_end),
            )
        )
        .order_by(TimeItem.due_at.is_(None), TimeItem.due_at, desc(TimeItem.priority), TimeItem.created_at)
        .limit(8)
        .all()
    )
    if urgent:
        return urgent
    return (
        db.query(TimeItem)
        .filter(TimeItem.status.in_(("open", "snoozed")))
        .order_by(TimeItem.due_at.is_(None), TimeItem.due_at, desc(TimeItem.priority), TimeItem.created_at)
        .limit(5)
        .all()
    )


def build_overview_card_payload(db: Session, summaries: list[ReflectionSummary]) -> dict[str, Any]:
    milestone_dicts = [serialize_reflection_summary(summary) for summary in summaries]
    brief = build_current_day_brief(db, summaries)
    urgent_items = [time_item_to_card_item(item) for item in urgent_items_for_overview(db)]
    return {
        "generated_at": brief["generated_at"],
        "brief_title": brief["headline"],
        "brief_message": brief["message"],
        "brief_payload": brief,
        "milestones": milestone_dicts,
        "urgent_items": urgent_items,
        "metrics": {
            "brief_confidence": brief["confidence"],
            "milestones": len(milestone_dicts),
            "urgent_items": len(urgent_items),
        },
        "sections": [
            {"title": "Brief", "items": [brief]},
            {"title": "Milestones", "items": milestone_dicts},
            {"title": "Urgent items", "items": urgent_items},
        ],
        "evidence": [*brief["evidence"], *[item for summary in milestone_dicts for item in summary.get("evidence", [])[:1]]],
    }


def persist_dashboard_card(
    db: Session,
    card: dict[str, Any],
    *,
    create_report: bool = False,
    report_date: date | None = None,
) -> DashboardCard:
    db.query(DashboardCard).filter(DashboardCard.mode == card["mode"], DashboardCard.status == "active").update({"status": "archived"})
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


def refresh_overview_card(db: Session, *, anchor_date: date | None = None) -> DashboardCard:
    summaries = ensure_reflection_summaries(db, anchor_date=anchor_date)
    payload = build_overview_card_payload(db, summaries)
    card = {
        "mode": "overview",
        "card_type": "reflection_overview",
        "title": payload["brief_title"],
        "summary": payload["brief_message"],
        "priority": 95,
        "metrics": payload["metrics"],
        "sections": payload["sections"],
        "brief_title": payload["brief_title"],
        "brief_message": payload["brief_message"],
        "brief_payload": payload["brief_payload"],
        "milestones": payload["milestones"],
        "urgent_items": payload["urgent_items"],
        "generated_at": payload["generated_at"],
        "evidence": payload["evidence"],
    }
    return persist_dashboard_card(db, card, create_report=False)


def ensure_overview_card(db: Session) -> DashboardCard:
    card = (
        db.query(DashboardCard)
        .filter(DashboardCard.mode == "overview", DashboardCard.status == "active")
        .order_by(desc(DashboardCard.created_at))
        .first()
    )
    if card:
        return card
    return refresh_overview_card(db)


def refresh_dashboard_cards(
    db: Session,
    *,
    modes: list[str] | tuple[str, ...] | None = None,
    use_llm: bool = False,
    create_reports: bool = False,
) -> dict[str, DashboardCard]:
    del modes, use_llm, create_reports
    return {"overview": refresh_overview_card(db)}


def ensure_dashboard_cards(db: Session) -> dict[str, DashboardCard]:
    return {"overview": ensure_overview_card(db)}


def card_to_dict(card: DashboardCard) -> dict[str, Any]:
    payload = card.payload or {}
    return {
        "id": card.id,
        "mode": card.mode,
        "card_type": card.card_type,
        "title": card.title,
        "summary": card.summary,
        "priority": card.priority,
        "brief_title": payload.get("brief_title") or card.title,
        "brief_message": payload.get("brief_message") or card.summary,
        "brief_payload": payload.get("brief_payload", {}),
        "metrics": payload.get("metrics", {}),
        "sections": payload.get("sections", []),
        "milestones": payload.get("milestones", []),
        "urgent_items": payload.get("urgent_items", []),
        "generated_at": payload.get("generated_at") or card.created_at.isoformat(),
        "evidence": card.evidence,
        "created_at": card.created_at.isoformat(),
    }


def infer_persona_memories_from_message(db: Session, message: ChatMessage) -> list[Memory]:
    return infer_persona_memories_from_text(db, message.content, evidence=[{"chat_message_id": message.id}])


def persona_group_name(kind: str) -> str:
    mapping = {
        "trait": "traits",
        "preference": "preferences",
        "goal": "goals",
        "wellbeing_signal": "wellbeing_signals",
        "diet_response": "health_patterns",
        "health_pattern": "health_patterns",
        "work_style": "work_style",
        "self_belief": "wellbeing_signals",
    }
    return mapping.get(kind, "other")


def persona_stable_profile(persona: PersonaProfile) -> dict[str, Any]:
    profile = {**default_persona_profile(), **(persona.profile or {})}
    return {
        "gender": persona.gender,
        "name": profile["name"],
    }


def grouped_persona_memories(db: Session) -> dict[str, list[dict[str, Any]]]:
    memories = (
        db.query(Memory)
        .filter(Memory.superseded_by_id.is_(None))
        .order_by(desc(Memory.confidence), desc(Memory.updated_at))
        .all()
    )
    groups: dict[str, list[dict[str, Any]]] = {
        "traits": [],
        "preferences": [],
        "goals": [],
        "health_patterns": [],
        "work_style": [],
        "wellbeing_signals": [],
        "other": [],
    }
    for memory in memories:
        groups[persona_group_name(memory.kind)].append(
            {
                "id": memory.id,
                "kind": memory.kind,
                "content": memory.content,
                "confidence": memory.confidence,
                "evidence": memory.evidence,
                "updated_at": memory.updated_at.isoformat(),
            }
        )
    return groups


def inferred_persona_profile_summary(db: Session, persona: PersonaProfile | None = None) -> dict[str, str]:
    persona = persona or ensure_persona(db)
    groups = grouped_persona_memories(db)
    profile = {**default_persona_profile(), **(persona.profile or {})}
    name = profile.get("name") or "you"
    gender = persona.gender or "unspecified"

    identity_parts = [f"LifeOS knows {name} as a self-building profile."]
    if gender and gender != "unspecified":
        identity_parts.append(f"Gender currently set to {gender}.")
    identity_parts.append(persona_summary_line(groups["traits"], "There are not enough durable trait signals yet."))

    wellbeing = persona_summary_line(
        groups["wellbeing_signals"] + groups["health_patterns"],
        "No stable wellbeing baseline has been inferred yet.",
    )
    focus = persona_summary_line(groups["goals"], "Goals are still emerging from your logs and chats.")
    prefs = persona_summary_line(
        groups["preferences"] + groups["work_style"],
        "Preferences and work style will appear here as you keep chatting.",
    )
    return {
        "identity": " ".join(identity_parts),
        "wellbeing_baseline": wellbeing,
        "focus_and_goals": focus,
        "preferences_and_work_style": prefs,
    }


def run_job(db: Session, job_name: str) -> AgentRun:
    run = AgentRun(
        job_name=job_name,
        mode=None,
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
            card = refresh_overview_card(db)
            run.output_card_ids = [card.id]
            run.summary = f"Processed {count} pending entries and refreshed overview."
        elif job_name == "overview_refresh":
            card = refresh_overview_card(db)
            run.output_card_ids = [card.id]
            run.summary = "Rebuilt the active overview card."
        elif job_name == "summary_rollup":
            summaries = generate_reflection_summaries(db, anchor_date=latest_completed_local_day(), use_llm=False)
            card = refresh_overview_card(db, anchor_date=latest_completed_local_day())
            run.output_card_ids = [card.id]
            run.summary = f"Generated {len(summaries)} milestone summaries and refreshed overview."
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


def format_local_timestamp(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return ""
    local_value = db_utc(value).astimezone(local_tz(timezone_name))
    return local_value.strftime("%Y-%m-%d %H:%M")


def history_sources(context: HistoricalContext) -> list[dict[str, Any]]:
    return [
        *[
            {"type": "raw_entry", "id": entry.id, "occurred_at": entry.occurred_at.isoformat(), "text": entry.text}
            for entry in context.logs
        ],
        *[
            {
                "type": "event",
                "id": event.id,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "summary": event.summary,
            }
            for event in context.events
        ],
        *[
            {
                "type": "time_item",
                "id": item.id,
                "kind": item.kind,
                "status": item.status,
                "due_at": item.due_at.isoformat() if item.due_at else None,
                "starts_at": item.starts_at.isoformat() if item.starts_at else None,
                "title": item.title,
            }
            for item in context.time_items
        ],
        *[
            {
                "type": "chat_message",
                "id": message.id,
                "role": message.role,
                "created_at": message.created_at.isoformat(),
                "content": message.content,
            }
            for message in context.chat_messages
        ],
        *[
            {
                "type": "reflection_summary",
                "id": summary.id,
                "period_key": summary.period_key,
                "anchor_date": summary.anchor_date.isoformat(),
                "title": summary.title,
                "body": summary.body,
            }
            for summary in context.reflection_summaries
        ],
        *[
            {
                "type": "memory",
                "id": memory.id,
                "kind": memory.kind,
                "confidence": memory.confidence,
                "content": memory.content,
            }
            for memory in context.memories
        ],
    ]


def context_has_data(context: HistoricalContext) -> bool:
    return bool(context.logs or context.events or context.time_items or context.chat_messages)


def context_counts(context: HistoricalContext) -> dict[str, int]:
    return {
        "logs": len(context.logs),
        "events": len(context.events),
        "time_items": len(context.time_items),
        "chat_messages": len(context.chat_messages),
        "reflection_summaries": len(context.reflection_summaries),
        "memories": len(context.memories),
    }


def render_historical_timeline(context: HistoricalContext, timezone_name: str) -> str:
    timeline: list[tuple[datetime, str]] = []
    for entry in context.logs:
        timeline.append((db_utc(entry.occurred_at) or entry.occurred_at, f"{format_local_timestamp(entry.occurred_at, timezone_name)} [log] {entry.text}"))
    for event in context.events:
        timeline.append((db_utc(event.occurred_at) or event.occurred_at, f"{format_local_timestamp(event.occurred_at, timezone_name)} [{event.event_type}] {event.summary}"))
    for item in context.time_items:
        when = item.due_at or item.starts_at
        if when:
            timeline.append((db_utc(when) or when, f"{format_local_timestamp(when, timezone_name)} [{item.kind}] {item.title}"))
    for message in context.chat_messages:
        timeline.append(
            (
                db_utc(message.created_at) or message.created_at,
                f"{format_local_timestamp(message.created_at, timezone_name)} [chat:{message.role}] {message.content[:200]}",
            )
        )
    timeline.sort(key=lambda item: item[0])
    if not timeline:
        return ""
    return "\n".join(line for _when, line in timeline[:24])


def historical_context_payload(context: HistoricalContext) -> dict[str, Any]:
    return {
        "query_type": "historical_analysis",
        "resolved_window_start": context.window.start_utc.isoformat(),
        "resolved_window_end": context.window.end_utc.isoformat(),
        "local_window_label": context.window.label,
        "logs": [
            {"id": entry.id, "occurred_at": entry.occurred_at.isoformat(), "text": entry.text}
            for entry in context.logs[:20]
        ],
        "events": [
            {"id": event.id, "event_type": event.event_type, "occurred_at": event.occurred_at.isoformat(), "summary": event.summary}
            for event in context.events[:24]
        ],
        "time_items": [
            {
                "id": item.id,
                "kind": item.kind,
                "title": item.title,
                "status": item.status,
                "due_at": item.due_at.isoformat() if item.due_at else None,
                "starts_at": item.starts_at.isoformat() if item.starts_at else None,
            }
            for item in context.time_items[:24]
        ],
        "chat_messages": [
            {"id": message.id, "role": message.role, "created_at": message.created_at.isoformat(), "content": message.content[:240]}
            for message in context.chat_messages[:18]
        ],
        "reflection_summaries": [
            {"id": summary.id, "period_key": summary.period_key, "title": summary.title, "body": summary.body}
            for summary in context.reflection_summaries[:10]
        ],
        "memories": [
            {"id": memory.id, "kind": memory.kind, "confidence": memory.confidence, "content": memory.content}
            for memory in context.memories[:12]
        ],
    }


def answer_historical_facts(context: HistoricalContext, timezone_name: str) -> str:
    counts = context_counts(context)
    if not context_has_data(context):
        return f"I could not find any logs, events, tasks, chat messages, or summaries for {context.window.label}."
    lead = (
        f"Here is the grounded record for {context.window.label}: "
        f"{counts['logs']} logs, {counts['events']} events, {counts['time_items']} time items, "
        f"and {counts['chat_messages']} chat messages."
    )
    timeline = render_historical_timeline(context, timezone_name)
    if timeline:
        return lead + "\n" + timeline
    return lead


def fallback_historical_analysis(
    message: str,
    context: HistoricalContext,
    timezone_name: str,
    *,
    comparison: HistoricalContext | None = None,
) -> str:
    primary_counts = context_counts(context)
    if comparison:
        comparison_counts = context_counts(comparison)
        return (
            f"Grounded comparison for {context.window.label} versus {comparison.window.label}: "
            f"{context.window.label} has {primary_counts['logs']} logs, {primary_counts['events']} events, "
            f"and {primary_counts['time_items']} time items; "
            f"{comparison.window.label} has {comparison_counts['logs']} logs, {comparison_counts['events']} events, "
            f"and {comparison_counts['time_items']} time items. "
            "The database supports a comparison, but Ollama is unavailable for deeper synthesis right now."
        )
    timeline = render_historical_timeline(context, timezone_name)
    lead = (
        f"Grounded analysis input for {context.window.label}: "
        f"{primary_counts['logs']} logs, {primary_counts['events']} events, {primary_counts['time_items']} time items, "
        f"and {primary_counts['chat_messages']} chat messages."
    )
    if timeline:
        return lead + "\n" + timeline
    return lead + " The window is sparse, so any analysis would be low confidence."


def historical_analysis_messages(
    message: str,
    context: HistoricalContext,
    *,
    comparison: HistoricalContext | None = None,
) -> list[dict[str, str]]:
    payload = {"primary": historical_context_payload(context)}
    if comparison:
        payload["comparison"] = historical_context_payload(comparison)
    system = (
        "You are LifeOS, a local personal assistant. Analyze only the supplied historical database context. "
        "Ground every claim in the evidence. If the window is sparse, say so. "
        "For factual references, do not invent missing events."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Historical context:\n{payload}\n\nQuestion:\n{message}"},
    ]


def semantic_answer_messages(message: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "You are LifeOS, a local personal assistant. Answer using the supplied database context. "
        "If a factual question requires dates or exact history and the context is insufficient, say what data is missing. "
        "Be practical and concise. Avoid medical diagnosis; frame health advice as patterns to test."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Database context:\n{payload}\n\nQuestion:\n{message}"},
    ]


def stream_text_chunks(text: str, *, chunk_size: int = 160) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        if end < len(text):
            split = text.rfind(" ", start, end)
            if split > start + 20:
                end = split + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def answer_historical_analysis(
    db: Session,
    message: str,
    context: HistoricalContext,
    timezone_name: str,
    *,
    comparison: HistoricalContext | None = None,
) -> str:
    if not context_has_data(context) and not (comparison and context_has_data(comparison)):
        return f"I could not find enough grounded data to analyze {context.window.label}."
    try:
        return get_llm().chat(historical_analysis_messages(message, context, comparison=comparison), temperature=0.1)
    except Exception:
        return fallback_historical_analysis(message, context, timezone_name, comparison=comparison)


def stream_chat_turn_events(
    db: Session,
    message: str,
    *,
    session_id: int | None = None,
) -> Any:
    session, _user_message, created_items = prepare_chat_turn(db, message, session_id=session_id)
    yield {"event": "session", "data": {"session_id": session.id}}
    prefix_text, prefix_sources = created_item_prefix(created_items)
    if created_items:
        yield {
            "event": "working_note",
            "data": {"text": f"Captured {len(created_items)} time item{'s' if len(created_items) != 1 else ''} while I work on the reply."},
        }

    def finish_with_text(answer_text: str, extra_sources: list[dict[str, Any]]) -> Any:
        full_answer = prefix_text + answer_text
        all_sources = prefix_sources + extra_sources
        yield {"event": "answer_start", "data": {}}
        for chunk in stream_text_chunks(full_answer):
            yield {"event": "answer_delta", "data": {"text": chunk}}
        persist_assistant_turn(db, session, full_answer, all_sources)
        yield {"event": "sources", "data": {"items": all_sources}}
        yield {"event": "done", "data": {}}

    try:
        persona = ensure_persona(db)
        timezone_name = persona.timezone or settings.default_timezone
        yield {
            "event": "working_note",
            "data": {"text": "Checking whether this is a time-based question or a general context question."},
        }
        comparison_windows = parse_comparison_time_windows(message, timezone_name=timezone_name) if is_temporal_query_intent(message) else None
        primary_window = parse_time_window(message, timezone_name=timezone_name) if is_temporal_query_intent(message) else None

        if comparison_windows:
            yield {"event": "working_note", "data": {"text": "Retrieving both historical windows from your database."}}
            left_context = historical_context(db, comparison_windows[0], include_memories=is_analytical_history_question(message))
            right_context = historical_context(db, comparison_windows[1], include_memories=is_analytical_history_question(message))
            history = history_sources(left_context) + history_sources(right_context)
            if is_analytical_history_question(message):
                yield {"event": "working_note", "data": {"text": "Analyzing the grounded comparison before drafting the answer."}}
                if not context_has_data(left_context) and not context_has_data(right_context):
                    yield from finish_with_text(
                        f"I could not find enough grounded data to analyze {left_context.window.label}.",
                        history,
                    )
                    return
                final_parts = [prefix_text]
                yield {"event": "answer_start", "data": {}}
                if prefix_text:
                    for chunk in stream_text_chunks(prefix_text):
                        yield {"event": "answer_delta", "data": {"text": chunk}}
                streamed = False
                try:
                    for delta in get_llm().chat_stream(
                        historical_analysis_messages(message, left_context, comparison=right_context),
                        temperature=0.1,
                    ):
                        streamed = True
                        final_parts.append(delta)
                        yield {"event": "answer_delta", "data": {"text": delta}}
                except Exception:
                    fallback = fallback_historical_analysis(message, left_context, timezone_name, comparison=right_context)
                    if not streamed:
                        final_parts = [prefix_text, fallback]
                        for chunk in stream_text_chunks(fallback):
                            yield {"event": "answer_delta", "data": {"text": chunk}}
                full_answer = "".join(final_parts)
                all_sources = prefix_sources + history
                persist_assistant_turn(db, session, full_answer, all_sources)
                yield {"event": "sources", "data": {"items": all_sources}}
                yield {"event": "done", "data": {}}
                return
            left_answer = answer_historical_facts(left_context, timezone_name)
            right_answer = answer_historical_facts(right_context, timezone_name)
            yield from finish_with_text(left_answer + "\n\n" + right_answer, history)
            return

        if primary_window:
            yield {"event": "working_note", "data": {"text": "Retrieving the requested historical window from your database."}}
            context = historical_context(db, primary_window, include_memories=is_analytical_history_question(message))
            history = history_sources(context)
            if is_analytical_history_question(message):
                yield {"event": "working_note", "data": {"text": "Analyzing the grounded historical context before drafting the answer."}}
                if not context_has_data(context):
                    yield from finish_with_text(f"I could not find enough grounded data to analyze {context.window.label}.", history)
                    return
                final_parts = [prefix_text]
                yield {"event": "answer_start", "data": {}}
                if prefix_text:
                    for chunk in stream_text_chunks(prefix_text):
                        yield {"event": "answer_delta", "data": {"text": chunk}}
                streamed = False
                try:
                    for delta in get_llm().chat_stream(historical_analysis_messages(message, context), temperature=0.1):
                        streamed = True
                        final_parts.append(delta)
                        yield {"event": "answer_delta", "data": {"text": delta}}
                except Exception:
                    fallback = fallback_historical_analysis(message, context, timezone_name)
                    if not streamed:
                        final_parts = [prefix_text, fallback]
                        for chunk in stream_text_chunks(fallback):
                            yield {"event": "answer_delta", "data": {"text": chunk}}
                full_answer = "".join(final_parts)
                all_sources = prefix_sources + history
                persist_assistant_turn(db, session, full_answer, all_sources)
                yield {"event": "sources", "data": {"items": all_sources}}
                yield {"event": "done", "data": {}}
                return
            yield from finish_with_text(answer_historical_facts(context, timezone_name), history)
            return

        yield {"event": "working_note", "data": {"text": "Retrieving relevant memories, recent context, and open loops."}}
        semantic = semantic_search(db, message, limit=6)
        context = recent_context(db)
        context["open_time_items"] = [
            time_item_to_card_item(item)
            for item in db.query(TimeItem).filter(TimeItem.status.in_(("open", "snoozed"))).order_by(TimeItem.due_at).limit(20).all()
        ]
        payload = {"semantic_matches": semantic, "recent_context": context}
        yield {"event": "working_note", "data": {"text": "Drafting the final answer from your local context."}}
        final_parts = [prefix_text]
        yield {"event": "answer_start", "data": {}}
        if prefix_text:
            for chunk in stream_text_chunks(prefix_text):
                yield {"event": "answer_delta", "data": {"text": chunk}}
        streamed = False
        try:
            for delta in get_llm().chat_stream(semantic_answer_messages(message, payload), temperature=0.2):
                streamed = True
                final_parts.append(delta)
                yield {"event": "answer_delta", "data": {"text": delta}}
        except Exception:
            if semantic:
                fallback = "I found related local context, but Ollama is not reachable yet:\n" + "\n".join(
                    f"- {item['content']}" for item in semantic[:4]
                )
            else:
                fallback = "I do not have enough local context yet, and Ollama is not reachable. Add logs or start Ollama, then ask again."
            if not streamed:
                final_parts = [prefix_text, fallback]
                for chunk in stream_text_chunks(fallback):
                    yield {"event": "answer_delta", "data": {"text": chunk}}
        full_answer = "".join(final_parts)
        all_sources = prefix_sources + semantic
        persist_assistant_turn(db, session, full_answer, all_sources)
        yield {"event": "sources", "data": {"items": all_sources}}
        yield {"event": "done", "data": {}}
    except Exception as exc:
        db.rollback()
        yield {"event": "error", "data": {"message": str(exc)}}


def answer_chat(db: Session, message: str) -> tuple[str, list[dict]]:
    persona = ensure_persona(db)
    timezone_name = persona.timezone or settings.default_timezone
    comparison_windows = parse_comparison_time_windows(message, timezone_name=timezone_name) if is_temporal_query_intent(message) else None
    primary_window = parse_time_window(message, timezone_name=timezone_name) if is_temporal_query_intent(message) else None
    if comparison_windows:
        left_context = historical_context(db, comparison_windows[0], include_memories=is_analytical_history_question(message))
        right_context = historical_context(db, comparison_windows[1], include_memories=is_analytical_history_question(message))
        sources = history_sources(left_context) + history_sources(right_context)
        if is_analytical_history_question(message):
            return answer_historical_analysis(db, message, left_context, timezone_name, comparison=right_context), sources
        left_answer = answer_historical_facts(left_context, timezone_name)
        right_answer = answer_historical_facts(right_context, timezone_name)
        return left_answer + "\n\n" + right_answer, sources
    if primary_window:
        context = historical_context(db, primary_window, include_memories=is_analytical_history_question(message))
        sources = history_sources(context)
        if is_analytical_history_question(message):
            return answer_historical_analysis(db, message, context, timezone_name), sources
        return answer_historical_facts(context, timezone_name), sources

    semantic = semantic_search(db, message, limit=6)
    context = recent_context(db)
    context["open_time_items"] = [
        time_item_to_card_item(item)
        for item in db.query(TimeItem).filter(TimeItem.status.in_(("open", "snoozed"))).order_by(TimeItem.due_at).limit(20).all()
    ]
    sources = semantic
    payload = {"semantic_matches": semantic, "recent_context": context}
    try:
        answer = get_llm().chat(semantic_answer_messages(message, payload), temperature=0.2)
    except Exception:
        if semantic:
            bullets = "\n".join(f"- {item['content']}" for item in semantic[:4])
            answer = f"I found related local context, but Ollama is not reachable yet:\n{bullets}"
        else:
            answer = "I do not have enough local context yet, and Ollama is not reachable. Add logs or start Ollama, then ask again."
    return answer, sources

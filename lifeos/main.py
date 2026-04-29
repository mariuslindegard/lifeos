from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc
from sqlalchemy.orm import Session

from lifeos.agent import (
    CARD_ORDER,
    card_to_dict,
    create_raw_entry,
    ensure_dashboard_cards,
    ensure_persona,
    record_chat_turn,
    refresh_dashboard_cards,
    run_job,
)
from lifeos.auth import clear_session_cookie, require_user, set_session_cookie, verify_password
from lifeos.db import get_db, init_db
from lifeos.models import AgentRun, ChatMessage, ChatSession, DailyReport, DashboardCard, ExtractedEvent, Memory, RawEntry, Recommendation, TimeItem
from lifeos.schemas import ChatRequest, ChatResponse, LoginRequest, RawEntryCreate, RawEntryOut, SnoozeRequest
from lifeos.scheduler import start_scheduler, stop_scheduler

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    db = next(get_db())
    try:
        ensure_persona(db)
    finally:
        db.close()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="LifeOS", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.post("/api/auth/login")
def login(payload: LoginRequest, response: Response) -> dict:
    if not verify_password(payload.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")
    set_session_cookie(response)
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
def me(_user=Depends(require_user)) -> dict:
    return {"authenticated": True, "user": "owner"}


@app.post("/api/logs", response_model=RawEntryOut)
def add_log(payload: RawEntryCreate, db: Session = Depends(get_db), _user=Depends(require_user)) -> RawEntry:
    return create_raw_entry(
        db,
        text_value=payload.text,
        source=payload.source,
        occurred_at=payload.occurred_at,
        metadata=payload.metadata,
    )


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    cards = ensure_dashboard_cards(db)
    entries = db.query(RawEntry).order_by(desc(RawEntry.occurred_at)).limit(15).all()
    events = db.query(ExtractedEvent).order_by(desc(ExtractedEvent.occurred_at)).limit(30).all()
    time_items = db.query(TimeItem).order_by(TimeItem.due_at.is_(None), TimeItem.due_at, desc(TimeItem.priority)).limit(30).all()
    chat_messages = db.query(ChatMessage).order_by(desc(ChatMessage.created_at)).limit(20).all()
    memories = (
        db.query(Memory)
        .filter(Memory.superseded_by_id.is_(None))
        .order_by(desc(Memory.confidence), desc(Memory.updated_at))
        .limit(12)
        .all()
    )
    recommendations = (
        db.query(Recommendation).filter(Recommendation.status == "active").order_by(desc(Recommendation.created_at)).limit(5).all()
    )
    runs = db.query(AgentRun).order_by(desc(AgentRun.started_at)).limit(5).all()
    return {
        "card_order": list(CARD_ORDER),
        "cards": {mode: card_to_dict(cards[mode]) for mode in CARD_ORDER if mode in cards},
        "entries": [
            {
                "id": entry.id,
                "text": entry.text,
                "source": entry.source,
                "occurred_at": entry.occurred_at.isoformat(),
                "processing_status": entry.processing_status,
            }
            for entry in entries
        ],
        "events": [
            {
                "id": event.id,
                "raw_entry_id": event.raw_entry_id,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "summary": event.summary,
                "attributes": event.attributes,
            }
            for event in events
        ],
        "time_items": [
            {
                "id": item.id,
                "kind": item.kind,
                "title": item.title,
                "notes": item.notes,
                "status": item.status,
                "priority": item.priority,
                "due_at": item.due_at.isoformat() if item.due_at else None,
                "starts_at": item.starts_at.isoformat() if item.starts_at else None,
                "ends_at": item.ends_at.isoformat() if item.ends_at else None,
            }
            for item in time_items
        ],
        "chat_messages": [
            {
                "id": message.id,
                "session_id": message.session_id,
                "role": message.role,
                "content": message.content,
                "sources": message.sources,
                "analysis_status": message.analysis_status,
                "created_at": message.created_at.isoformat(),
            }
            for message in chat_messages
        ],
        "memories": [
            {
                "id": memory.id,
                "kind": memory.kind,
                "content": memory.content,
                "confidence": memory.confidence,
                "evidence": memory.evidence,
            }
            for memory in memories
        ],
        "recommendations": [
            {
                "id": recommendation.id,
                "title": recommendation.title,
                "body": recommendation.body,
                "status": recommendation.status,
                "evidence": recommendation.evidence,
            }
            for recommendation in recommendations
        ],
        "agent_runs": [
            {
                "id": run.id,
                "job_name": run.job_name,
                "mode": run.mode,
                "status": run.status,
                "summary": run.summary,
                "error": run.error,
                "input_message_ids": run.input_message_ids,
                "output_card_ids": run.output_card_ids,
                "started_at": run.started_at.isoformat(),
            }
            for run in runs
        ],
    }


@app.get("/api/memories")
def list_memories(db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    memories = db.query(Memory).order_by(desc(Memory.updated_at)).all()
    return {"memories": [{"id": item.id, "kind": item.kind, "content": item.content, "confidence": item.confidence} for item in memories]}


def chat_message_out(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "session_id": message.session_id,
        "role": message.role,
        "content": message.content,
        "sources": message.sources,
        "analysis_status": message.analysis_status,
        "metadata": message.metadata_,
        "created_at": message.created_at.isoformat(),
    }


@app.get("/api/chat/history")
def chat_history(session_id: int | None = None, db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    if session_id:
        session = db.get(ChatSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Chat session not found")
        messages = db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at).all()
        return {
            "session": {
                "id": session.id,
                "title": session.title,
                "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
                "created_at": session.created_at.isoformat(),
            },
            "messages": [chat_message_out(message) for message in messages],
        }

    sessions = db.query(ChatSession).order_by(desc(ChatSession.last_message_at), desc(ChatSession.created_at)).limit(30).all()
    items = []
    for session in sessions:
        last_message = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == session.id)
            .order_by(desc(ChatMessage.created_at))
            .first()
        )
        count = db.query(ChatMessage).filter(ChatMessage.session_id == session.id).count()
        items.append(
            {
                "id": session.id,
                "title": session.title,
                "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
                "created_at": session.created_at.isoformat(),
                "message_count": count,
                "preview": last_message.content[:160] if last_message else "",
            }
        )
    return {"sessions": items}


@app.post("/api/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, db: Session = Depends(get_db), _user=Depends(require_user)) -> ChatResponse:
    answer, sources, session_id = record_chat_turn(db, payload.message, session_id=payload.session_id)
    return ChatResponse(answer=answer, session_id=session_id, sources=sources)


@app.post("/api/agent/run/{job_name}")
def trigger_agent(job_name: str, db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    if job_name not in {"ingest", "hourly_analysis", "daily_execution", "self_analysis", "life_journal", "persona_refresh", "reflect", "nightly"}:
        raise HTTPException(status_code=404, detail="Unknown job")
    run = run_job(db, job_name)
    return {
        "id": run.id,
        "job_name": run.job_name,
        "mode": run.mode,
        "status": run.status,
        "summary": run.summary,
        "error": run.error,
        "input_message_ids": run.input_message_ids,
        "output_card_ids": run.output_card_ids,
    }


@app.get("/api/cards/{card_id}/history")
def card_history(card_id: str, db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    if card_id.isdigit():
        card = db.get(DashboardCard, int(card_id))
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        mode = card.mode
    else:
        mode = card_id
    if mode not in CARD_ORDER:
        raise HTTPException(status_code=404, detail="Unknown card mode")
    cards = (
        db.query(DashboardCard)
        .filter(DashboardCard.mode == mode)
        .order_by(desc(DashboardCard.created_at))
        .limit(30)
        .all()
    )
    reports = (
        db.query(DailyReport)
        .filter(DailyReport.mode == mode)
        .order_by(desc(DailyReport.report_date), desc(DailyReport.created_at))
        .limit(30)
        .all()
    )
    return {
        "mode": mode,
        "cards": [card_to_dict(card) for card in cards],
        "reports": [
            {
                "id": report.id,
                "mode": report.mode,
                "report_date": report.report_date.isoformat(),
                "title": report.title,
                "body": report.body,
                "payload": report.payload,
                "evidence": report.evidence,
                "created_at": report.created_at.isoformat(),
            }
            for report in reports
        ],
    }


@app.post("/api/time-items/{item_id}/complete")
def complete_time_item(item_id: int, db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    item = db.get(TimeItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Time item not found")
    item.status = "complete"
    db.commit()
    refresh_dashboard_cards(db, modes=("execution",))
    return {"id": item.id, "status": item.status}


@app.post("/api/time-items/{item_id}/snooze")
def snooze_time_item(
    item_id: int,
    payload: SnoozeRequest | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    item = db.get(TimeItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Time item not found")
    days = payload.days if payload else 1
    base = item.due_at or item.starts_at or datetime.now(timezone.utc)
    item.due_at = base + timedelta(days=days)
    if item.starts_at:
        item.starts_at = item.starts_at + timedelta(days=days)
    if item.ends_at:
        item.ends_at = item.ends_at + timedelta(days=days)
    item.status = "snoozed"
    db.commit()
    refresh_dashboard_cards(db, modes=("execution",))
    return {"id": item.id, "status": item.status, "due_at": item.due_at.isoformat() if item.due_at else None}

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc
from sqlalchemy.orm import Session

from lifeos.agent import (
    create_raw_entry,
    ensure_overview_card,
    ensure_persona,
    ensure_reflection_summaries,
    grouped_persona_memories,
    latest_completed_local_day,
    persona_stable_profile,
    record_chat_turn,
    refresh_overview_card,
    run_job,
)
from lifeos.auth import clear_session_cookie, require_user, set_session_cookie, verify_password
from lifeos.db import get_db, init_db
from lifeos.models import AgentRun, ChatMessage, ChatSession, DashboardCard, ReflectionSummary, TimeItem
from lifeos.schemas import ChatRequest, ChatResponse, LoginRequest, PersonaStableProfileUpdate, RawEntryCreate, RawEntryOut, SnoozeRequest
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


app = FastAPI(title="LifeOS", version="0.2.0", lifespan=lifespan)
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
def add_log(payload: RawEntryCreate, db: Session = Depends(get_db), _user=Depends(require_user)):
    return create_raw_entry(
        db,
        text_value=payload.text,
        source=payload.source,
        occurred_at=payload.occurred_at,
        metadata=payload.metadata,
    )


def latest_run_payload(db: Session) -> dict | None:
    run = db.query(AgentRun).order_by(desc(AgentRun.started_at)).first()
    if not run:
        return None
    return {
        "id": run.id,
        "job_name": run.job_name,
        "mode": run.mode,
        "status": run.status,
        "summary": run.summary,
        "error": run.error,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@app.get("/api/overview")
def overview(db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    anchor_date = latest_completed_local_day()
    summaries = ensure_reflection_summaries(db, anchor_date=anchor_date)
    card = ensure_overview_card(db)
    payload = card.payload or {}
    return {
        "generated_at": payload.get("generated_at") or card.created_at.isoformat(),
        "card_title": payload.get("card_title") or card.title,
        "card_message": payload.get("card_message") or card.summary,
        "milestones": payload.get("milestones") or [
            {
                "id": summary.id,
                "period_key": summary.period_key,
                "anchor_date": summary.anchor_date.isoformat(),
                "window_start": summary.window_start.isoformat(),
                "window_end": summary.window_end.isoformat(),
                "title": summary.title,
                "body": summary.body,
                "headline": (summary.payload or {}).get("headline", summary.title),
                "narrative": (summary.payload or {}).get("narrative", summary.body),
                "wins": (summary.payload or {}).get("wins", []),
                "risks": (summary.payload or {}).get("risks", []),
                "patterns": (summary.payload or {}).get("patterns", []),
                "carry_forward_points": (summary.payload or {}).get("carry_forward_points", []),
                "open_loops": (summary.payload or {}).get("open_loops", []),
                "metrics": (summary.payload or {}).get("metrics", {}),
                "evidence": summary.evidence,
                "source_summary_ids": summary.source_summary_ids,
                "created_at": summary.created_at.isoformat(),
            }
            for summary in summaries
        ],
        "urgent_items": payload.get("urgent_items", []),
        "latest_run": latest_run_payload(db),
    }


@app.get("/api/overview/history")
def overview_history(db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    cards = (
        db.query(DashboardCard)
        .filter(DashboardCard.mode == "overview")
        .order_by(desc(DashboardCard.created_at))
        .limit(20)
        .all()
    )
    summaries = (
        db.query(ReflectionSummary)
        .order_by(desc(ReflectionSummary.anchor_date), desc(ReflectionSummary.created_at))
        .limit(100)
        .all()
    )
    anchors: dict[str, list[dict]] = {}
    for summary in summaries:
        anchors.setdefault(summary.anchor_date.isoformat(), []).append(
            {
                "id": summary.id,
                "period_key": summary.period_key,
                "title": summary.title,
                "body": summary.body,
                "created_at": summary.created_at.isoformat(),
            }
        )
    return {
        "cards": [
            {
                "id": card.id,
                "title": card.title,
                "summary": card.summary,
                "created_at": card.created_at.isoformat(),
            }
            for card in cards
        ],
        "anchors": [
            {"anchor_date": anchor_date, "summaries": items}
            for anchor_date, items in anchors.items()
        ],
    }


@app.get("/api/persona")
def persona(db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    profile = ensure_persona(db)
    return {
        "stable_profile": persona_stable_profile(profile),
        "inferred_groups": grouped_persona_memories(db),
        "updated_at": profile.updated_at.isoformat(),
    }


@app.patch("/api/persona")
def update_persona(
    payload: PersonaStableProfileUpdate,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    persona = ensure_persona(db)
    updates = payload.model_dump(exclude_unset=True)
    profile = dict(persona.profile or {})

    for field in ("birth_year", "gender", "locale", "timezone"):
        if field in updates:
            setattr(persona, field, updates[field])

    list_fields = {"focus_areas", "values", "preferences", "constraints", "goals"}
    profile_fields = {
        "name",
        "life_stage",
        "personality_summary",
        "wellbeing_baseline",
        "focus_areas",
        "values",
        "preferences",
        "constraints",
        "goals",
    }
    for field in profile_fields:
        if field not in updates:
            continue
        if field in list_fields:
            profile[field] = list(updates[field] or [])
        else:
            profile[field] = updates[field] or ""

    persona.profile = profile
    persona.goals = list(profile.get("goals") or [])
    db.commit()
    refresh_overview_card(db)
    db.refresh(persona)
    return {
        "stable_profile": persona_stable_profile(persona),
        "inferred_groups": grouped_persona_memories(db),
        "updated_at": persona.updated_at.isoformat(),
    }


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
    allowed = {
        "ingest",
        "overview_refresh",
        "summary_rollup",
    }
    if job_name not in allowed:
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


@app.post("/api/time-items/{item_id}/complete")
def complete_time_item(item_id: int, db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    item = db.get(TimeItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Time item not found")
    item.status = "complete"
    db.commit()
    refresh_overview_card(db)
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
    refresh_overview_card(db)
    return {"id": item.id, "status": item.status, "due_at": item.due_at.isoformat() if item.due_at else None}

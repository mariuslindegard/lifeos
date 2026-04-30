import os
import tempfile
import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/lifeos-test.db"
os.environ["LIFEOS_PASSWORD"] = "test-password"
os.environ["LIFEOS_SECRET_KEY"] = "test-secret"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["LIFEOS_ENV"] = "development"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:1"

from fastapi.testclient import TestClient
from ollama import _types as ollama_types
from sqlalchemy import inspect

from lifeos.agent import add_memory, latest_completed_local_day
from lifeos.db import SessionLocal, engine
from lifeos.llm import OllamaClient
from lifeos.main import app
from lifeos.models import AgentRun, ChatMessage, ChatSession, DashboardCard, ExtractedEvent, Memory, RawEntry, ReflectionSummary, TimeItem
from lifeos.rag import parse_time_window
from lifeos.scheduler import scheduler, start_scheduler, stop_scheduler

OSLO = ZoneInfo("Europe/Oslo")


def login(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"password": "test-password"})
    assert response.status_code == 200


def local_noon_days_ago(days: int) -> datetime:
    target = datetime.now(timezone.utc).astimezone(OSLO).date() - timedelta(days=days)
    return datetime.combine(target, time(12, 0), tzinfo=OSLO).astimezone(timezone.utc)


def latest_anchor_noon() -> datetime:
    anchor = latest_completed_local_day()
    return datetime(anchor.year, anchor.month, anchor.day, 12, 0, tzinfo=timezone.utc)


def clear_runtime_rows() -> None:
    db = SessionLocal()
    try:
        for model in (AgentRun, ChatMessage, ChatSession, DashboardCard, ExtractedEvent, RawEntry, TimeItem, ReflectionSummary, Memory):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def parse_sse_frames(raw_text: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in raw_text.split("\n\n"):
        if not block.strip():
            continue
        event_name = "message"
        data = "{}"
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        frames.append((event_name, json.loads(data)))
    return frames


def test_ollama_stream_reader_supports_typed_chat_response_objects() -> None:
    client = OllamaClient()

    def fake_chat(**_kwargs):
        return iter(
            [
                ollama_types.ChatResponse(message=ollama_types.Message(role="assistant", content="Hello ")),
                ollama_types.ChatResponse(message=ollama_types.Message(role="assistant", content="world")),
            ]
        )

    client.client.chat = fake_chat
    assert "".join(client.chat_stream([{"role": "user", "content": "Hi"}])) == "Hello world"


def test_ollama_stream_reader_surfaces_thinking_and_content() -> None:
    client = OllamaClient()

    def fake_chat(**_kwargs):
        return iter(
            [
                ollama_types.ChatResponse(message=ollama_types.Message(role="assistant", content="", thinking="Need context.")),
                ollama_types.ChatResponse(message=ollama_types.Message(role="assistant", content="Hello", thinking=None)),
            ]
        )

    client.client.chat = fake_chat
    assert list(client.chat_stream_events([{"role": "user", "content": "Hi"}])) == [
        {"thinking": "Need context.", "content": ""},
        {"thinking": "", "content": "Hello"},
    ]


def test_auth_required_and_overview_endpoint() -> None:
    with TestClient(app) as client:
        assert client.get("/api/overview").status_code == 401
        login(client)
        response = client.get("/api/overview")
        assert response.status_code == 200
        data = response.json()
        assert data["brief_title"]
        assert data["brief_message"] == "Begynn å chatte for å gi meg noe å analysere!"
        assert [item["period_key"] for item in data["milestones"]] == [
            "yesterday",
            "past_7_days",
            "past_30_days",
            "past_6_months",
            "past_1_year",
        ]


def test_reflection_summary_table_exists_and_migration_file_present() -> None:
    inspector = inspect(engine)
    assert "reflection_summaries" in inspector.get_table_names()
    assert Path("alembic/versions/0004_reflection_summaries.py").exists()


def test_temporal_parser_supports_relative_windows_and_ranges() -> None:
    now = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)

    relative = parse_time_window("what did I do 8 days ago?", now=now, timezone_name="Europe/Oslo")
    assert relative is not None
    assert relative.start_local_date.isoformat() == "2026-04-21"
    assert relative.end_local_date.isoformat() == "2026-04-21"
    assert relative.is_single_day is True

    last_week = parse_time_window("what happened last week?", now=now, timezone_name="Europe/Oslo")
    assert last_week is not None
    assert last_week.start_local_date.isoformat() == "2026-04-20"
    assert last_week.end_local_date.isoformat() == "2026-04-26"

    bounded = parse_time_window("between March 1 and March 10", now=now, timezone_name="Europe/Oslo")
    assert bounded is not None
    assert bounded.start_local_date.isoformat() == "2026-03-01"
    assert bounded.end_local_date.isoformat() == "2026-03-10"

    explicit = parse_time_window("2026-04-21", now=now, timezone_name="Europe/Oslo")
    assert explicit is not None
    assert explicit.start_local_date.isoformat() == "2026-04-21"


def test_historical_facts_query_uses_grounded_db_retrieval(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [1.0, 0.0, 0.0])

    with TestClient(app) as client:
        login(client)
        client.post(
            "/api/logs",
            json={
                "text": "Had oats and coffee, then worked a focused sales block.",
                "occurred_at": local_noon_days_ago(8).isoformat(),
            },
        )

        response = client.post("/api/chat", json={"message": "What did I do 8 days ago?"})
        assert response.status_code == 200
        answer = response.json()["answer"]
        assert "grounded record" in answer.lower()
        assert "oats" in answer.lower()
        source_types = {item["type"] for item in response.json()["sources"]}
        assert {"raw_entry", "event"}.issubset(source_types)


def test_historical_analysis_query_uses_same_window_with_grounded_fallback(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.4, 0.4, 0.4])

    with TestClient(app) as client:
        login(client)
        client.post(
            "/api/logs",
            json={
                "text": "Low energy after lunch and coffee, then focus recovered after a walk.",
                "occurred_at": local_noon_days_ago(8).isoformat(),
            },
        )
        response = client.post("/api/chat", json={"message": "How was my energy 8 days ago?"})
        assert response.status_code == 200
        answer = response.json()["answer"].lower()
        assert "grounded analysis" in answer or "historical context" in answer or "grounded analysis input" in answer
        assert "semantic" not in answer


def test_historical_comparison_question_compares_named_windows(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.3, 0.3, 0.3])

    with TestClient(app) as client:
        login(client)
        this_week_day = datetime.now(timezone.utc).astimezone(OSLO).date() - timedelta(days=1)
        last_week_day = this_week_day - timedelta(days=7)
        client.post(
            "/api/logs",
            json={
                "text": "This week I felt stable and energetic.",
                "occurred_at": datetime.combine(this_week_day, time(12, 0), tzinfo=OSLO).astimezone(timezone.utc).isoformat(),
            },
        )
        client.post(
            "/api/logs",
            json={
                "text": "Last week I felt tired and unfocused.",
                "occurred_at": datetime.combine(last_week_day, time(12, 0), tzinfo=OSLO).astimezone(timezone.utc).isoformat(),
            },
        )
        response = client.post("/api/chat", json={"message": "What changed between last week and this week?"})
        assert response.status_code == 200
        answer = response.json()["answer"]
        assert "last week" in answer.lower()
        assert "this week" in answer.lower()


def test_empty_historical_window_reports_sparse_data(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.1, 0.1, 0.1])

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/chat", json={"message": "What did I do 2 months ago?"})
        assert response.status_code == 200
        assert "could not find" in response.json()["answer"].lower()


def test_persona_patch_updates_stable_profile_without_mutating_memories(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.1, 0.2, 0.3])

    with TestClient(app) as client:
        login(client)
        db = SessionLocal()
        try:
            add_memory(db, kind="trait", content="User prefers focused mornings.", confidence=0.5, evidence=[{"raw_entry_id": 1}])
            db.commit()
            before = db.query(Memory).count()
        finally:
            db.close()

        response = client.patch(
            "/api/persona",
            json={
                "gender": "male",
                "name": "Marius",
                "focus_areas": ["health", "work"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["stable_profile"]["name"] == "Marius"
        assert data["stable_profile"]["gender"] == "male"
        assert "focus_areas" not in data["stable_profile"]
        assert data["inferred_profile_summary"]["identity"]

        db = SessionLocal()
        try:
            after = db.query(Memory).count()
            assert before == after
        finally:
            db.close()


def test_direct_persona_inference_still_works_from_chat_and_logs(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.2, 0.4, 0.6])

    with TestClient(app) as client:
        login(client)
        client.post("/api/chat", json={"message": "I prefer quiet mornings and my goals are consistent routines."})
        client.post(
            "/api/logs",
            json={
                "text": "I am more focused after a long morning walk.",
                "occurred_at": latest_anchor_noon().isoformat(),
            },
        )
        persona = client.get("/api/persona")
        assert persona.status_code == 200
        payload = persona.json()
        groups = payload["inferred_groups"]
        assert groups["preferences"] or groups["goals"] or groups["traits"]
        assert payload["inferred_profile_summary"]["preferences_and_work_style"]


def test_chat_session_rolls_after_six_hours(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.0, 0.0, 1.0])

    with TestClient(app) as client:
        login(client)
        first = client.post("/api/chat", json={"message": "I need to plan tomorrow."}).json()
        old_session_id = first["session_id"]

        db = SessionLocal()
        try:
            session = db.get(ChatSession, old_session_id)
            session.last_message_at = datetime.now(timezone.utc) - timedelta(hours=7)
            db.commit()
        finally:
            db.close()

        second = client.post("/api/chat", json={"message": "Starting a new thought.", "session_id": old_session_id}).json()
        assert second["session_id"] != old_session_id


def test_removed_mode_jobs_return_404_and_core_jobs_still_work(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.2, 0.2, 0.2])

    with TestClient(app) as client:
        login(client)
        for job_name in ["hourly_analysis", "daily_execution", "self_analysis", "life_journal", "persona_refresh", "reflect", "nightly"]:
            response = client.post(f"/api/agent/run/{job_name}")
            assert response.status_code == 404

        client.post("/api/logs", json={"text": "Had coffee late and focus crashed.", "occurred_at": latest_anchor_noon().isoformat()})
        response = client.post("/api/agent/run/summary_rollup")
        assert response.status_code == 200
        assert response.json()["status"] == "success"

        db = SessionLocal()
        try:
            anchor = latest_completed_local_day()
            summaries = db.query(ReflectionSummary).filter(ReflectionSummary.anchor_date == anchor).all()
            assert len(summaries) == 5
        finally:
            db.close()


def test_overview_returns_live_brief_when_today_has_signal(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.5, 0.1, 0.4])
    clear_runtime_rows()

    with TestClient(app) as client:
        login(client)
        client.post("/api/chat", json={"message": "Need to prepare for a client meeting and review notes today."})
        response = client.get("/api/overview")
        assert response.status_code == 200
        data = response.json()
        assert data["brief_title"].startswith("Dagens brief")
        assert data["brief_message"] != "Begynn å chatte for å gi meg noe å analysere!"
        assert "Tracked across" not in data["brief_message"]


def test_overview_empty_state_uses_exact_norwegian_message() -> None:
    clear_runtime_rows()
    with TestClient(app) as client:
        login(client)
        response = client.get("/api/overview")
        assert response.status_code == 200
        assert response.json()["brief_message"] == "Begynn å chatte for å gi meg noe å analysere!"


def test_overview_refresh_scheduler_runs_every_two_hours(monkeypatch) -> None:
    scheduler.remove_all_jobs()
    if scheduler.running:
        stop_scheduler()
    monkeypatch.setattr("lifeos.scheduler.settings.scheduler_enabled", True)
    start_scheduler()
    try:
        job = scheduler.get_job("overview_refresh")
        assert job is not None
        assert getattr(job.trigger.interval, "total_seconds")() == 7200
    finally:
        stop_scheduler()
        scheduler.remove_all_jobs()


def test_chat_history_time_item_actions_and_message_storage_still_work(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [1.0, 1.0, 0.0])

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/chat", json={"message": "Remind me tomorrow at 9 to review tax documents."})
        assert response.status_code == 200
        assert "Added to Daily Execution" in response.json()["answer"]

        sessions = client.get("/api/chat/history")
        assert sessions.status_code == 200
        assert sessions.json()["sessions"]

        overview = client.get("/api/overview").json()
        item_id = overview["urgent_items"][0]["time_item_id"]
        complete = client.post(f"/api/time-items/{item_id}/complete")
        assert complete.status_code == 200
        assert complete.json()["status"] == "complete"

        db = SessionLocal()
        try:
            assert db.query(ChatMessage).count() >= 2
            assert db.query(TimeItem).count() >= 1
            assert db.query(AgentRun).count() >= 0
        finally:
            db.close()


def test_chat_stream_endpoint_emits_working_notes_then_answer_and_persists_final_message(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.7, 0.1, 0.2])
    monkeypatch.setattr(
        "lifeos.llm.OllamaClient.chat_stream_events",
        lambda self, messages, temperature=0.2, think=True: iter(
            [
                {"thinking": "Checking context.", "content": ""},
                {"thinking": "", "content": "Here is "},
                {"thinking": "", "content": "a streamed reply."},
            ]
        ),
    )

    body = ""
    with TestClient(app) as client:
        clear_runtime_rows()
        login(client)
        with client.stream("POST", "/api/chat/stream", json={"message": "Summarize my current context."}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())

        frames = parse_sse_frames(body)
        event_names = [event for event, _payload in frames]
        assert event_names[0] == "session"
        assert "working_note" in event_names
        assert "thinking_delta" in event_names
        assert event_names.index("answer_start") < event_names.index("answer_delta")
        assert event_names[-2:] == ["sources", "done"]

    db = SessionLocal()
    try:
        messages = db.query(ChatMessage).order_by(ChatMessage.created_at).all()
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert messages[1].content == "Here is a streamed reply."
        assert "working" not in messages[1].content.lower()
    finally:
        db.close()


def test_chat_stream_supports_deterministic_history_answers_without_persisting_notes(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [1.0, 0.0, 0.0])

    body = ""
    with TestClient(app) as client:
        clear_runtime_rows()
        login(client)
        client.post(
            "/api/logs",
            json={
                "text": "Had oats and coffee, then worked a focused block.",
                "occurred_at": local_noon_days_ago(8).isoformat(),
            },
        )
        with client.stream("POST", "/api/chat/stream", json={"message": "What did I do 8 days ago?"}) as response:
            body = "".join(response.iter_text())
        frames = parse_sse_frames(body)
        event_names = [event for event, _payload in frames]
        assert "working_note" in event_names
        assert "answer_start" in event_names
        answer_text = "".join(payload.get("text", "") for event, payload in frames if event == "answer_delta")
        assert "grounded record" in answer_text.lower()

    db = SessionLocal()
    try:
        session_messages = db.query(ChatMessage).order_by(ChatMessage.created_at).all()
        assert len(session_messages) >= 2
        assert all("working_note" not in str(item.metadata_ or {}) for item in session_messages)
        assert all(item.role != "assistant" or "grounded record" in item.content.lower() for item in session_messages if item.role == "assistant")
    finally:
        db.close()


def test_memory_update_increases_confidence(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.0, 1.0, 0.0])
    with TestClient(app):
        db = SessionLocal()
        try:
            before = (
                db.query(Memory)
                .filter(Memory.kind == "health_pattern", Memory.content == "Coffee after lunch may reduce sleep quality.")
                .count()
            )
            first = add_memory(
                db,
                kind="health_pattern",
                content="Coffee after lunch may reduce sleep quality.",
                confidence=0.4,
                evidence=[{"raw_entry_id": 1}],
            )
            second = add_memory(
                db,
                kind="health_pattern",
                content="Coffee after lunch may reduce sleep quality.",
                confidence=0.6,
                evidence=[{"raw_entry_id": 2}],
            )
            db.commit()
            assert first.id == second.id
            assert second.confidence > 0.6
            assert len(second.evidence) == 2
            after = (
                db.query(Memory)
                .filter(Memory.kind == "health_pattern", Memory.content == "Coffee after lunch may reduce sleep quality.")
                .count()
            )
            assert after == before + 1
        finally:
            db.close()


def test_frontend_markup_and_assets_match_new_shell() -> None:
    html = Path("static/index.html").read_text()
    js = Path("static/app.js").read_text()
    css = Path("static/styles.css").read_text()
    sw = Path("static/sw.js").read_text()

    assert 'id="overviewContent"' in html
    assert 'id="personaNavButton"' in html
    assert 'id="personaContent"' in html
    assert 'id="cardsGrid"' not in html
    assert "function renderOverview" in js
    assert "function renderPersona" in js
    assert "/api/chat/stream" in js
    assert "working-note" in js
    assert "Earlier Reflections" in js
    assert "Begynn å chatte for å gi meg noe å analysere!" in js
    assert "reflection-surface" in css
    assert "persona-layout" in css
    assert "history-section" in css
    assert "brief-grid" not in css
    assert 'CACHE_NAME = "lifeos-v11"' in sw

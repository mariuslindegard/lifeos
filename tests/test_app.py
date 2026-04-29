import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/lifeos-test.db"
os.environ["LIFEOS_PASSWORD"] = "test-password"
os.environ["LIFEOS_SECRET_KEY"] = "test-secret"
os.environ["SCHEDULER_ENABLED"] = "false"

from fastapi.testclient import TestClient

from lifeos.agent import add_memory
from lifeos.db import SessionLocal
from lifeos.main import app
from lifeos.agent import ANALYSIS_MODES
from lifeos.models import AgentRun, ChatMessage, ChatSession, DashboardCard, ExtractedEvent, Memory, TimeItem


def login(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"password": "test-password"})
    assert response.status_code == 200


def test_auth_required_and_login() -> None:
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 401
        login(client)
        response = client.get("/api/dashboard")
        assert response.status_code == 200
        assert "entries" in response.json()
        assert set(response.json()["cards"]) == {"execution", "analysis", "journal", "persona"}


def test_log_creation_extracts_event_and_chat_recalls_date(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [1.0, 0.0, 0.0])

    with TestClient(app) as client:
        login(client)
        occurred_at = datetime(2026, 4, 29, 12, 30, tzinfo=timezone.utc).isoformat()
        response = client.post(
            "/api/logs",
            json={
                "text": "Had oats and coffee, felt wired, then crashed after lunch.",
                "occurred_at": occurred_at,
            },
        )
        assert response.status_code == 200
        assert response.json()["processing_status"] == "processed"

        db = SessionLocal()
        try:
            event = db.query(ExtractedEvent).one()
            assert event.event_type == "meal"
            assert "oats" in event.summary
        finally:
            db.close()

        chat = client.post("/api/chat", json={"message": "What did I do on 2026-04-29?"})
        assert chat.status_code == 200
        assert "oats" in chat.json()["answer"]
        assert chat.json()["sources"][0]["type"] == "event"


def test_natural_language_reminder_creates_time_item_and_execution_card(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [1.0, 0.0, 0.0])

    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/logs",
            json={
                "text": "Remind me Friday at 10 to call the accountant.",
                "occurred_at": "2026-04-29T08:00:00+00:00",
            },
        )
        assert response.status_code == 200

        db = SessionLocal()
        try:
            item = db.query(TimeItem).order_by(TimeItem.id.desc()).first()
            assert item is not None
            assert item.kind == "reminder"
            assert "accountant" in item.title
        finally:
            db.close()

        dashboard = client.get("/api/dashboard").json()
        execution = dashboard["cards"]["execution"]
        assert execution["card_type"] == "daily_execution"
        assert any(
            "accountant" in item["label"]
            for section in execution["sections"]
            for item in section["items"]
        )


def test_memory_update_increases_confidence(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.0, 1.0, 0.0])
    with TestClient(app):
        db = SessionLocal()
        try:
            first = add_memory(
                db,
                kind="diet_response",
                content="Coffee after lunch may reduce sleep quality.",
                confidence=0.4,
                evidence=[{"raw_entry_id": 1}],
            )
            second = add_memory(
                db,
                kind="diet_response",
                content="Coffee after lunch may reduce sleep quality.",
                confidence=0.6,
                evidence=[{"raw_entry_id": 2}],
            )
            db.commit()
            assert first.id == second.id
            assert second.confidence > 0.6
            assert len(second.evidence) == 2
            assert db.query(Memory).count() == 1
        finally:
            db.close()


def test_manual_reflection_creates_recommendation(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.0, 0.0, 1.0])

    with TestClient(app) as client:
        login(client)
        client.post("/api/logs", json={"text": "Felt low energy after a large lunch and coffee."})
        response = client.post("/api/agent/run/reflect")
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["recommendations"]


def test_chat_persists_turn_and_creates_time_item(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.0, 0.0, 1.0])

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/chat", json={"message": "Deadline for tax documents is May 10."})
        assert response.status_code == 200
        assert response.json()["session_id"]
        assert "Added to Daily Execution" in response.json()["answer"]

        db = SessionLocal()
        try:
            user_message = db.query(ChatMessage).filter(ChatMessage.role == "user").order_by(ChatMessage.id.desc()).first()
            assistant_message = db.query(ChatMessage).filter(ChatMessage.role == "assistant").order_by(ChatMessage.id.desc()).first()
            assert user_message is not None
            assert assistant_message is not None
            assert user_message.session_id == assistant_message.session_id == response.json()["session_id"]
            assert user_message.analysis_status == "pending"
            item = db.query(TimeItem).filter(TimeItem.kind == "deadline").order_by(TimeItem.id.desc()).first()
            assert item is not None
            assert "tax documents" in item.title
        finally:
            db.close()


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


def test_chat_session_rolls_after_day_change(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.0, 0.0, 1.0])

    with TestClient(app) as client:
        login(client)
        first = client.post("/api/chat", json={"message": "Today had a useful sales session."}).json()
        old_session_id = first["session_id"]

        db = SessionLocal()
        try:
            session = db.get(ChatSession, old_session_id)
            session.last_message_at = datetime.now(timezone.utc) - timedelta(days=1)
            db.commit()
        finally:
            db.close()

        second = client.post("/api/chat", json={"message": "New day, new notes.", "session_id": old_session_id}).json()
        assert second["session_id"] != old_session_id


def test_hourly_analysis_cycles_modes_and_completes_message(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.4, 0.4, 0.4])

    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/chat",
            json={
                "message": "Today I have been out selling on doors. I ate well and felt energized, but a bit low self-esteem."
            },
        )
        assert response.status_code == 200

        modes = [client.post("/api/agent/run/hourly_analysis").json()["mode"] for _ in range(4)]
        assert modes == list(ANALYSIS_MODES)

        db = SessionLocal()
        try:
            runs = db.query(AgentRun).filter(AgentRun.job_name == "hourly_analysis").order_by(AgentRun.id.desc()).limit(4).all()
            assert all(run.input_message_ids is not None for run in runs)
            message = (
                db.query(ChatMessage)
                .filter(ChatMessage.role == "user", ChatMessage.content.like("%selling on doors%"))
                .order_by(ChatMessage.id.desc())
                .first()
            )
            assert message is not None
            assert message.analysis_status == "complete"
            modes_done = message.metadata_["analysis_modes"]
            assert all(modes_done[mode] for mode in ANALYSIS_MODES)
        finally:
            db.close()


def test_chat_history_endpoint(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.1, 0.2, 0.3])

    with TestClient(app) as client:
        login(client)
        chat = client.post("/api/chat", json={"message": "Log this in chat history."}).json()
        sessions = client.get("/api/chat/history")
        assert sessions.status_code == 200
        assert sessions.json()["sessions"]

        messages = client.get(f"/api/chat/history?session_id={chat['session_id']}")
        assert messages.status_code == 200
        assert any("Log this in chat history" in item["content"] for item in messages.json()["messages"])


def test_card_history_and_time_item_actions(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [1.0, 1.0, 0.0])

    with TestClient(app) as client:
        login(client)
        client.post("/api/logs", json={"text": "Need to review dashboard cards tomorrow."})
        dashboard = client.get("/api/dashboard").json()
        card_id = dashboard["cards"]["execution"]["id"]
        history = client.get(f"/api/cards/{card_id}/history")
        assert history.status_code == 200
        assert history.json()["mode"] == "execution"

        item_id = dashboard["cards"]["execution"]["sections"][0]["items"][0]["time_item_id"]
        complete = client.post(f"/api/time-items/{item_id}/complete")
        assert complete.status_code == 200
        assert complete.json()["status"] == "complete"

        client.post("/api/logs", json={"text": "Remind me tomorrow at 9 to test snooze."})
        dashboard = client.get("/api/dashboard").json()
        item_id = dashboard["cards"]["execution"]["sections"][0]["items"][0]["time_item_id"]
        snooze = client.post(f"/api/time-items/{item_id}/snooze", json={"days": 1})
        assert snooze.status_code == 200
        assert snooze.json()["status"] == "snoozed"


def test_nightly_generates_schema_cards_and_reports(monkeypatch) -> None:
    monkeypatch.setattr("lifeos.rag.vector_for_text", lambda _content: [0.2, 0.2, 0.2])

    with TestClient(app) as client:
        login(client)
        client.post("/api/logs", json={"text": "Had coffee late and focus crashed."})
        response = client.post("/api/agent/run/nightly")
        assert response.status_code == 200
        assert response.json()["status"] == "success"

        db = SessionLocal()
        try:
            modes = {card.mode for card in db.query(DashboardCard).all()}
            assert {"execution", "analysis", "journal", "persona"}.issubset(modes)
        finally:
            db.close()


def test_overview_markup_has_no_reflect_button_and_keeps_cards() -> None:
    html = Path("static/index.html").read_text()
    assert "Reflect" not in html
    assert 'id="cardsGrid"' in html
    assert "Chat History" in html

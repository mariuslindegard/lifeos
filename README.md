# LifeOS

LifeOS is a local-first personal operating system for natural-language logging, memory, and advice. It runs a FastAPI backend, SQLite database, dashboard-first PWA, APScheduler agent loop, and optional Ollama service.

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000`. The default development password is `changeme`; set `LIFEOS_PASSWORD` before using it on your LAN.

To run Ollama inside Compose:

```bash
docker compose --profile ollama up --build
```

If Ollama runs on the Ubuntu host instead, set `OLLAMA_BASE_URL` in `.env`.

## Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
uvicorn lifeos.main:app --reload
```

Run tests:

```bash
pytest
```

## Architecture

- `raw_entries` stores immutable natural-language input.
- `extracted_events` stores dynamic structured facts derived from raw logs.
- `time_items` stores tasks, reminders, deadlines, events, time blocks, and open loops extracted from natural language.
- `chat_sessions` and `chat_messages` persist both user messages and assistant responses for later analysis.
- `dashboard_cards` stores schema-generated UI cards for Daily Execution, Self Analysis, Life Journal, and Persona.
- `daily_reports` stores historical agent reports for each card mode.
- `persona_profile` stores a few stable fields plus flexible JSON profile data.
- `memories` stores long-term facts with confidence and evidence.
- `embeddings` stores local vectors as JSON for semantic recall.
- SQLite FTS5 powers exact text/date retrieval.
- Ollama powers chat, extraction, reflection, and embeddings when available.

The app is intentionally useful without a model running: logging, search, dashboard, and rule-based fallback extraction still work.

## Dynamic Dashboard

The LLM never writes frontend HTML. Agent jobs generate structured card JSON that the PWA renders through fixed components:

- Daily Execution: next actions, reminders, deadlines, overdue items.
- Self Analysis: diet, energy, mood, sleep, focus, and symptom signals.
- Life Journal: logs and chat history.
- Persona: stable profile fields and inferred memories.

Useful local examples:

```text
Remind me Friday at 10 to call the accountant.
Deadline for tax documents is May 10.
Tomorrow I need a 2 hour deep work block.
Had coffee late and my focus crashed.
```

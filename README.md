# LifeOS

LifeOS is a local-first personal operating system for natural-language logging, memory, and advice. It runs a FastAPI backend, SQLite database, reflection-first PWA, APScheduler agent loop, and optional Ollama service.

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000`. The default development password is `changeme`; set `LIFEOS_PASSWORD` before using it on your LAN.

To run Ollama inside Compose and pull the default chat and embedding models automatically:

```bash
docker compose --profile ollama up --build
```

The default chat model is `gemma4:e4b` and the default embedding model is `nomic-embed-text`.

If Ollama runs on the Ubuntu host instead, set `OLLAMA_BASE_URL=http://host.docker.internal:11434` in `.env`.

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
- `chat_sessions` and `chat_messages` persist both user messages and assistant responses for later retrieval and analysis.
- `dashboard_cards` stores the active Overview reflection card payload.
- `daily_reports` stores historical agent reports when generated.
- `reflection_summaries` stores rolling milestone summaries for yesterday, 7d, 30d, 6m, and 1y windows.
- `persona_profile` stores only the small user-managed stable profile surface plus flexible internal profile data.
- `memories` stores long-term facts with confidence and evidence.
- `embeddings` stores local vectors as JSON for semantic recall.
- SQLite FTS5 powers exact text/date retrieval.
- Ollama powers chat, extraction, reflection, and embeddings when available.

The app is intentionally useful without a model running: logging, grounded history queries, overview refresh, and rule-based fallback extraction still work.

## Overview And Persona

The LLM never writes frontend HTML. Agent jobs generate structured card JSON that the PWA renders through fixed components:

- Overview: one live current-day brief fed by today’s logs, chats, and upcoming items, with rolling milestone summaries tucked into history below it.
- Persona: only `name` and `gender` are editable; the rest of the profile is inferred and rendered as read-only summaries plus grouped memories.
- Chat: natural-language logging, reminders, and grounded historical analysis over data already stored in the database.

Useful local examples:

```text
Remind me Friday at 10 to call the accountant.
Deadline for tax documents is May 10.
Tomorrow I need a 2 hour deep work block.
Had coffee late and my focus crashed.
```

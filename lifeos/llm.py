import hashlib
import json
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

import ollama

from lifeos.config import settings


logger = logging.getLogger(__name__)


def _mapping_or_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    return {}


def _message_content(value: Any) -> str:
    payload = _mapping_or_dump(value)
    if payload:
        message = payload.get("message")
        if isinstance(message, dict):
            return str(message.get("content") or "")
    message = getattr(value, "message", None)
    content = getattr(message, "content", None)
    return str(content or "")


def _embedding_vector(value: Any) -> list[float]:
    payload = _mapping_or_dump(value)
    if isinstance(payload.get("embedding"), list):
        return payload["embedding"]
    embedding = getattr(value, "embedding", None)
    return embedding if isinstance(embedding, list) else []


class OllamaClient:
    def __init__(self) -> None:
        self.client = ollama.Client(host=settings.ollama_base_url)

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str:
        response = self.client.chat(
            model=settings.ollama_model,
            messages=messages,
            options={"temperature": temperature},
        )
        return _message_content(response)

    def chat_stream(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> Iterator[str]:
        response = self.client.chat(
            model=settings.ollama_model,
            messages=messages,
            stream=True,
            think=False,
            options={"temperature": temperature},
        )
        for chunk in response:
            content = _message_content(chunk)
            if content:
                yield content

    def embed(self, text: str) -> list[float]:
        response = self.client.embeddings(model=settings.ollama_embed_model, prompt=text)
        vector = _embedding_vector(response)
        if vector:
            return vector
        raise ValueError("Ollama embedding response did not include an embedding vector")


def get_llm() -> OllamaClient:
    return OllamaClient()


def wait_for_ollama_ready(timeout_seconds: int | None = None) -> None:
    timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.ollama_startup_timeout_seconds
    deadline = time.time() + max(1, timeout_seconds)
    client = ollama.Client(host=settings.ollama_base_url)
    last_error: Exception | None = None
    next_log_at = 0.0
    while time.time() < deadline:
        try:
            client.list()
            client.show(settings.ollama_model)
            client.show(settings.ollama_embed_model)
            logger.info(
                "Ollama ready at %s with chat model=%s embed model=%s",
                settings.ollama_base_url,
                settings.ollama_model,
                settings.ollama_embed_model,
            )
            return
        except Exception as exc:
            last_error = exc
            now = time.time()
            if now >= next_log_at:
                logger.warning(
                    "Waiting for Ollama at %s with chat model=%s embed model=%s: %s",
                    settings.ollama_base_url,
                    settings.ollama_model,
                    settings.ollama_embed_model,
                    exc,
                )
                next_log_at = now + 10
            time.sleep(2)
    detail = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown error"
    raise RuntimeError(
        "Ollama is required in production but is not ready. "
        f"base_url={settings.ollama_base_url} model={settings.ollama_model} "
        f"embed_model={settings.ollama_embed_model} detail={detail}"
    )


def fallback_embedding(text: str, dimensions: int = 64) -> list[float]:
    """Deterministic lexical embedding used when Ollama is unavailable."""
    vector = [0.0] * dimensions
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % dimensions
        vector[index] += 1.0
    magnitude = sum(value * value for value in vector) ** 0.5 or 1.0
    return [value / magnitude for value in vector]


def safe_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}

import hashlib
import json
import time
import re
from typing import Any

import ollama

from lifeos.config import settings


class OllamaClient:
    def __init__(self) -> None:
        self.client = ollama.Client(host=settings.ollama_base_url)

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str:
        response = self.client.chat(
            model=settings.ollama_model,
            messages=messages,
            options={"temperature": temperature},
        )
        return response["message"]["content"]

    def embed(self, text: str) -> list[float]:
        response = self.client.embeddings(model=settings.ollama_embed_model, prompt=text)
        return response["embedding"]


def get_llm() -> OllamaClient:
    return OllamaClient()


def wait_for_ollama_ready(timeout_seconds: int | None = None) -> None:
    timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.ollama_startup_timeout_seconds
    deadline = time.time() + max(1, timeout_seconds)
    client = ollama.Client(host=settings.ollama_base_url)
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            client.list()
            client.show(settings.ollama_model)
            client.show(settings.ollama_embed_model)
            return
        except Exception as exc:
            last_error = exc
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

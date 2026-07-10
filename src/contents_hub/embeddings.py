"""Optional embedding provider boundary for semantic query.

Mirrors the ``AgentRunner`` philosophy: the base install never requires an
embedding backend. ``CONTENTS_HUB_EMBEDDINGS`` selects the provider:

- unset / ``none`` — no semantic layer; query runs keyword-only.
- ``openai-compat`` — any OpenAI-compatible ``/embeddings`` endpoint
  (OpenAI, Ollama, LM Studio, llama.cpp, vLLM, ...):
  - ``CONTENTS_HUB_EMBEDDINGS_URL``   (default ``https://api.openai.com/v1``)
  - ``CONTENTS_HUB_EMBEDDINGS_MODEL`` (default ``text-embedding-3-small``)
  - ``CONTENTS_HUB_EMBEDDINGS_KEY``   (falls back to ``OPENAI_API_KEY``)

Vectors are stored as little-endian float32 blobs in ``item_embeddings``.
Whether embedded text leaves the machine is always explicit: the query
output lists which rankers ran.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from array import array
from typing import Protocol

logger = logging.getLogger(__name__)

_EMBED_TEXT_MAX_CHARS = 4000


class EmbeddingProvider(Protocol):
    name: str
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text."""
        ...


class OpenAICompatEmbeddings:
    """Client for any OpenAI-compatible ``POST {base}/embeddings`` endpoint."""

    name = "openai-compat"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        response = httpx.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": texts},
            headers=headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        if len(data) != len(texts):
            raise RuntimeError(
                f"embedding endpoint returned {len(data)} vectors for {len(texts)} inputs"
            )
        ordered = sorted(data, key=lambda entry: int(entry.get("index", 0)))
        return [list(map(float, entry["embedding"])) for entry in ordered]


def get_default_embedder() -> EmbeddingProvider | None:
    """Resolve the embedding provider from the environment (None = keyword-only)."""
    selection = (os.environ.get("CONTENTS_HUB_EMBEDDINGS") or "none").strip().lower()
    if selection in ("", "none"):
        return None
    if selection in ("openai-compat", "openai", "ollama"):
        base_url = (
            os.environ.get("CONTENTS_HUB_EMBEDDINGS_URL") or "https://api.openai.com/v1"
        )
        model = (
            os.environ.get("CONTENTS_HUB_EMBEDDINGS_MODEL") or "text-embedding-3-small"
        )
        api_key = (
            os.environ.get("CONTENTS_HUB_EMBEDDINGS_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        return OpenAICompatEmbeddings(base_url=base_url, model=model, api_key=api_key)
    logger.warning(
        "unknown CONTENTS_HUB_EMBEDDINGS=%r; semantic query disabled", selection
    )
    return None


def embedding_text(title: str, body: str, summary: str = "") -> str:
    """Canonical text an item is embedded from (title + best content)."""
    content = (summary or "").strip() or (body or "").strip()
    text = f"{(title or '').strip()}\n{content}".strip()
    return text[:_EMBED_TEXT_MAX_CHARS]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack_vector(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return list(values)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


__all__ = [
    "EmbeddingProvider",
    "OpenAICompatEmbeddings",
    "cosine_similarity",
    "embedding_text",
    "get_default_embedder",
    "pack_vector",
    "text_hash",
    "unpack_vector",
]

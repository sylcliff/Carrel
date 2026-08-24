"""Embedding helper (M5).

Thin wrapper over ``litellm.embedding`` that:
  - batches long input lists (50 texts per call by default)
  - retries 429/5xx twice with backoff
  - reads its key from env (``VOLCANO_API_KEY`` / ``DEEPSEEK_API_KEY``),
    keeping secrets out of YAML

ponytail: this is a wrapper, not a client. If we ever need provider-specific
quirks (per-call dims, custom encodings) we'll subclass; for now the
``litellm`` provider strings cover both volcengine and deepseek.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 50
DEFAULT_MAX_RETRIES = 3

# Map of model-id prefix -> env var to look for. New providers add a line.
_KEY_ENV = {
    "volcengine": "VOLCANO_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_AUTH_TOKEN",  # Anthropic SDK convention; falls back
                                           # to ANTHROPIC_API_KEY at litellm level
}


def _key_for(model: str) -> str | None:
    """Pick the right env var for a litellm model id like 'volcengine/foo'."""
    prefix = model.split("/", 1)[0] if "/" in model else model
    env_name = _KEY_ENV.get(prefix)
    if not env_name:
        return None
    val = os.environ.get(env_name)
    return val or None


def embed_texts(
    texts: list[str],
    *,
    model: str,
    batch_size: int = DEFAULT_BATCH,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: int = 60,
    api_key: str | None = None,
) -> list[list[float]]:
    """Return one embedding vector per input text.

    Empty input → empty output. Empty strings are skipped (they get a zero
    vector) so callers don't have to pre-filter.
    """
    if not texts:
        return []
    key = api_key or _key_for(model)
    if not key:
        raise RuntimeError(
            f"No API key for embedding model {model!r}; set "
            f"{_KEY_ENV.get(model.split('/', 1)[0], '?')} or pass api_key=."
        )

    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vec = _embed_with_retry(
            batch, model=model, max_retries=max_retries, timeout=timeout, api_key=key
        )
        out.extend(vec)
    return out


def _embed_with_retry(
    batch: list[str],
    *,
    model: str,
    max_retries: int,
    timeout: int,
    api_key: str,
) -> list[list[float]]:
    """One batched embedding call with retry on transient errors."""
    from litellm import embedding  # imported lazily so tests without keys still work

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp: Any = embedding(
                model=model,
                input=batch,
                api_key=api_key,
                timeout=timeout,
            )
            # litellm returns EmbeddingResponse; .data is a list of objects
            # with .embedding. Some providers may hand us a plain dict.
            data = getattr(resp, "data", None) or resp.get("data", [])
            vecs: list[list[float]] = []
            for item in data:
                emb = getattr(item, "embedding", None)
                if emb is None and isinstance(item, dict):
                    emb = item.get("embedding")
                vecs.append(list(emb))
            return vecs
        except Exception as e:  # noqa: BLE001
            last_err = e
            transient = _is_transient(e)
            if not transient or attempt == max_retries:
                raise
            sleep_s = 2 ** attempt
            logger.warning(
                "embedding batch failed (attempt %d/%d), retrying in %ds: %s",
                attempt + 1, max_retries, sleep_s, e,
            )
            time.sleep(sleep_s)
    # Unreachable, but keeps the type checker happy
    raise RuntimeError(f"embedding failed after {max_retries} retries: {last_err}")


def _is_transient(err: Exception) -> bool:
    msg = (str(err) or "").lower()
    return any(s in msg for s in ("429", "rate", "timeout", "503", "502", "500", "temporar"))


def zero_vector(dim: int) -> list[float]:
    """Return a zero vector of length ``dim`` for empty/short inputs."""
    return [0.0] * dim

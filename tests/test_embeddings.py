"""Tests for the embedding helper (litellm wrapper, batch, retry, key lookup)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from carrel import embeddings as emb


def test_zero_vector_shape():
    assert len(emb.zero_vector(8)) == 8
    assert emb.zero_vector(0) == []


def test_key_for_volcengine_uses_env(monkeypatch):
    monkeypatch.setenv("VOLCANO_API_KEY", "vk-test")
    assert emb._key_for("volcengine/doubao-embedding-large") == "vk-test"


def test_key_for_unknown_provider_returns_none(monkeypatch):
    monkeypatch.delenv("VOLCANO_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert emb._key_for("cohere/embed-v3") is None


def test_embed_texts_no_inputs_returns_empty():
    assert emb.embed_texts([], model="volcengine/foo") == []


def test_embed_texts_raises_without_key(monkeypatch):
    monkeypatch.delenv("VOLCANO_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="No API key"):
        emb.embed_texts(["hi"], model="volcengine/foo")


def test_embed_texts_batches_calls():
    """Batches >50 inputs into multiple litellm calls."""
    calls: list[list[str]] = []

    class _Resp:
        def __init__(self, batch: list[str]):
            self.data = [{"embedding": [float(i)] * 4} for i, _ in enumerate(batch)]

    def _fake_litellm(model, input, api_key, timeout):  # noqa: A002
        calls.append(list(input))
        return _Resp(list(input))

    with patch("litellm.embedding", side_effect=_fake_litellm):
        out = emb.embed_texts(
            [f"text-{i}" for i in range(120)],
            model="volcengine/foo",
            api_key="k",
            batch_size=50,
        )
    assert len(calls) == 3  # 50, 50, 20
    assert [len(c) for c in calls] == [50, 50, 20]
    assert len(out) == 120
    assert all(len(v) == 4 for v in out)


def test_embed_texts_retries_on_transient_then_succeeds():
    attempts = {"n": 0}

    class _Resp:
        def __init__(self, batch):
            self.data = [{"embedding": [0.1, 0.2, 0.3]} for _ in batch]

    def _flaky(model, input, api_key, timeout):  # noqa: A002
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("HTTP 429 rate limit")
        return _Resp(list(input))

    with patch("litellm.embedding", side_effect=_flaky):
        with patch("time.sleep"):  # don't actually sleep
            out = emb.embed_texts(
                ["a", "b"], model="volcengine/foo", api_key="k", max_retries=2
            )
    assert attempts["n"] == 2
    assert len(out) == 2


def test_embed_texts_gives_up_after_max_retries():
    def _always_fail(model, input, api_key, timeout):  # noqa: A002
        raise RuntimeError("HTTP 500 internal error")

    with patch("litellm.embedding", side_effect=_always_fail):
        with patch("time.sleep"):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                emb.embed_texts(["x"], model="volcengine/foo", api_key="k", max_retries=1)


def test_is_transient_detects_common_signals():
    assert emb._is_transient(RuntimeError("HTTP 429 rate limit")) is True
    assert emb._is_transient(RuntimeError("timeout waiting")) is True
    assert emb._is_transient(RuntimeError("503 service unavailable")) is True
    assert emb._is_transient(RuntimeError("HTTP 500 internal")) is True
    assert emb._is_transient(RuntimeError("invalid api key")) is False

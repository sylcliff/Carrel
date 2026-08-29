"""Tests for the paper card pipeline and API."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperStatus
from carrel.pipeline import paper_card as pipe
from sqlmodel import Session, SQLModel, create_engine


def _make_paper(session, **kw) -> Paper:
    base = dict(
        id="W1",
        id_kind="openalex",
        title="LoRA",
        status=PaperStatus.parsed.value,
        oa_status="oa",
        source="openalex",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(kw)
    p = Paper(**base)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _write_md(cfg, paper, body):
    cfg.storage.root.mkdir(parents=True, exist_ok=True)
    rel = f"papers/{paper.id}/paper.md"
    full = cfg.storage.root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    paper.md_path = rel
    return rel


def _fake_llm_payload(**overrides):
    """Return a default valid LLM payload (a dict, like chat_json returns)."""
    payload = {
        "research_question": "How can we adapt large language models cheaply?",
        "motivation": "Full fine-tuning is expensive.",
        "method_name": "LoRA",
        "method_summary": "Inject trainable low-rank decomposition matrices into each layer.",
        "key_techniques": ["low-rank decomposition", "frozen backbone"],
        "datasets": ["GLUE", "WikiSQL"],
        "baselines": ["Full Fine-tuning", "Adapter"],
        "code_url": "https://github.com/microsoft/LoRA",
        "main_results": [
            {"claim": "Matches full FT quality on GPT-3 175B", "value": None, "dataset": "GLUE"},
            {"claim": "Reduces trainable params by 10000x", "value": 10000.0, "unit": "x"},
        ],
        "metrics": ["perplexity", "accuracy"],
        "conclusion": "Low-rank adaptation matches full FT at a fraction of the cost.",
        "limitations": ["No RLHF study"],
        "future_work": ["Apply to other modalities"],
        "paper_type": "research",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def _fake_llm(payload=None, raises=None):
    """Return a chat_json stub that returns ``payload`` (or raises)."""
    data = _fake_llm_payload() if payload is None else _fake_llm_payload(**payload)

    def _chat(messages, **kwargs):
        if raises is not None:
            raise raises
        return data

    return _chat


@pytest.fixture(autouse=True)
def _has_key(monkeypatch):
    monkeypatch.setattr(pipe.llm, "has_key_for", lambda model: True)


@pytest.fixture(autouse=True)
def _wire_app_engine(session, monkeypatch):
    """Point ``carrel.db.app_engine`` at the test session's engine.

    The :func:`carrel.prompts_runtime.get_app_engine` lookup is what
    happens when the test session is the only DB context — we need the
    same engine so the resolver finds (or doesn't find) overrides
    consistently. The conftest's session fixture is the source of truth.
    """
    import carrel.db as _db
    _db.app_engine = session.get_bind()


class TestCoerceCard:
    def test_full_payload(self):
        card = pipe._coerce_card(_fake_llm_payload())
        assert card.method_name == "LoRA"
        assert card.paper_type.value == "research"
        assert card.confidence == 0.9
        assert len(card.main_results) == 2
        big = next(r for r in card.main_results if r.value == 10000.0)
        assert big.unit == "x"

    def test_drops_garbage(self):
        # String confidence is dropped by the actual coercion (it only
        # accepts int/float). Update: 0.0 default.  Strings anywhere
        # else get dropped too.
        card = pipe._coerce_card({
            "research_question": "  ",
            "method_summary": None,
            "key_techniques": "not a list",
            "main_results": "junk",
            "paper_type": "SurVey",
            "confidence": "0.7",
        })
        assert card.research_question is None
        assert card.method_summary is None
        assert card.key_techniques == []
        assert card.main_results == []
        assert card.paper_type.value == "survey"
        assert card.confidence == 0.0  # string confidence is dropped

    def test_paper_type_unknown_falls_back_to_other(self):
        card = pipe._coerce_card({"paper_type": "rfc-1234"})
        assert card.paper_type.value == "other"

    def test_clamps_confidence(self):
        assert pipe._coerce_card({"confidence": 5.0}).confidence == 1.0
        assert pipe._coerce_card({"confidence": -1.0}).confidence == 0.0

    def test_dedupes_list_fields(self):
        # The actual logic lowercases for the dedup key, so
        # "LoRA" and "lora" collide but "LoRA." does NOT collide with
        # "LoRA" (different lowercased strings).
        card = pipe._coerce_card({
            "key_techniques": ["LoRA", "lora", "LoRA."],
            "datasets": ["GLUE", "GLUE", "WikiSQL"],
        })
        assert card.key_techniques == ["LoRA", "LoRA."]
        assert card.datasets == ["GLUE", "WikiSQL"]


class TestExtractPaperCard:
    def test_happy_path(self, session, cfg, tmp_path, monkeypatch):
        cfg.storage.root = tmp_path / "data"
        p = _make_paper(session, md_path="papers/W1/paper.md")
        _write_md(
            cfg, p,
            "# Introduction\n\nLoRA enables cheap fine-tuning. " * 20
            + "\n\n# Conclusion\n\nWe match full FT at 10000x fewer params.",
        )
        monkeypatch.setattr(pipe.llm, "chat_json", _fake_llm())

        out = pipe.extract_paper_card(session, cfg, p.id)
        session.refresh(out)

        assert out.paper_card is not None
        assert out.paper_card_extracted_at is not None
        assert out.paper_card["method_name"] == "LoRA"
        assert out.paper_card["paper_type"] == "research"
        assert out.paper_card["confidence"] == 0.9

    def test_idempotent_when_fresh(self, session, cfg, tmp_path, monkeypatch):
        cfg.storage.root = tmp_path / "data"
        p = _make_paper(session, md_path="papers/W1/paper.md")
        _write_md(cfg, p, "# Intro\n\nSome body. " * 20)

        calls = {"n": 0}
        def _counting(messages, **kwargs):
            calls["n"] += 1
            return _fake_llm()(messages)
        monkeypatch.setattr(pipe.llm, "chat_json", _counting)
        pipe.extract_paper_card(session, cfg, p.id)

        # Mark the paper as fresh: updated_at == extracted_at, so a
        # second call should short-circuit.
        session.refresh(p)
        p.updated_at = p.paper_card_extracted_at
        session.add(p)
        session.commit()

        pipe.extract_paper_card(session, cfg, p.id)
        assert calls["n"] == 1

    def test_force_overrides(self, session, cfg, tmp_path, monkeypatch):
        cfg.storage.root = tmp_path / "data"
        p = _make_paper(session, md_path="papers/W1/paper.md")
        _write_md(cfg, p, "# Intro\n\nSome body. " * 20)
        monkeypatch.setattr(pipe.llm, "chat_json", _fake_llm())

        pipe.extract_paper_card(session, cfg, p.id)
        session.refresh(p)
        first_extracted_at = p.paper_card_extracted_at

        import time
        time.sleep(0.01)
        monkeypatch.setattr(pipe.llm, "chat_json", _fake_llm({"method_name": "LoRA v2"}))
        pipe.extract_paper_card(session, cfg, p.id, force=True)
        session.refresh(p)
        assert p.paper_card["method_name"] == "LoRA v2"
        assert p.paper_card_extracted_at > first_extracted_at

    def test_no_md_path_raises(self, session, cfg):
        p = _make_paper(session)
        with pytest.raises(pipe.PaperCardError, match="no md_path"):
            pipe.extract_paper_card(session, cfg, p.id)

    def test_missing_markdown_raises(self, session, cfg, tmp_path):
        cfg.storage.root = tmp_path / "data"
        p = _make_paper(session, md_path="papers/W1/missing.md")
        with pytest.raises(pipe.PaperCardError, match="missing on disk"):
            pipe.extract_paper_card(session, cfg, p.id)

    def test_too_short_body_raises(self, session, cfg, tmp_path):
        cfg.storage.root = tmp_path / "data"
        p = _make_paper(session, md_path="papers/W1/paper.md")
        _write_md(cfg, p, "too short")
        with pytest.raises(pipe.PaperCardError, match="too short"):
            pipe.extract_paper_card(session, cfg, p.id)

    def test_llm_error_wrapped(self, session, cfg, tmp_path, monkeypatch):
        from carrel.llm import LLMError
        cfg.storage.root = tmp_path / "data"
        p = _make_paper(session, md_path="papers/W1/paper.md")
        _write_md(cfg, p, "# Intro\n\nSome body. " * 20)
        monkeypatch.setattr(
            pipe.llm, "chat_json",
            _fake_llm(raises=LLMError("upstream 500")),
        )
        with pytest.raises(pipe.PaperCardError, match="upstream 500"):
            pipe.extract_paper_card(session, cfg, p.id)

        session.refresh(p)
        # Non-fatal: paper row keeps its prior state.
        assert p.paper_card is None
        assert p.paper_card_extracted_at is None


@pytest.fixture()
def _patch_app_config(monkeypatch, tmp_path):
    cfg = CarrelYAML()
    cfg.storage.root = tmp_path / "data"
    monkeypatch.setattr("carrel.main.app_config", cfg)
    return cfg


class TestPaperCardAPI:
    def test_get_returns_204_when_no_card(self, client, session, _patch_app_config):
        _make_paper(session)
        r = client.get("/papers/W1/card")
        assert r.status_code == 204
        assert r.headers.get("cache-control") == "no-store"

    def test_get_returns_404_for_unknown_paper(self, client, _patch_app_config):
        r = client.get("/papers/missing/card")
        assert r.status_code == 404

    def test_post_then_get_round_trips(self, client, session, _patch_app_config, tmp_path, monkeypatch):
        from carrel.pipeline import paper_card as pipe_mod
        p = _make_paper(session, md_path="papers/W1/paper.md")
        _write_md(_patch_app_config, p, "# Intro\n\nLoRA body. " * 30)
        monkeypatch.setattr(pipe_mod.llm, "chat_json", _fake_llm())

        r = client.post("/papers/W1/card/extract", json={"force": False})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["method_name"] == "LoRA"
        assert body["paper_type"] == "research"
        assert body["confidence"] == 0.9

        r2 = client.get("/papers/W1/card")
        assert r2.status_code == 200
        assert r2.json()["method_name"] == "LoRA"
        assert r2.headers.get("etag")

    def test_get_returns_304_when_etag_matches(self, client, session, _patch_app_config, tmp_path, monkeypatch):
        from carrel.pipeline import paper_card as pipe_mod
        p = _make_paper(session, md_path="papers/W1/paper.md")
        _write_md(_patch_app_config, p, "# Intro\n\nLoRA body. " * 30)
        monkeypatch.setattr(pipe_mod.llm, "chat_json", _fake_llm())

        client.post("/papers/W1/card/extract", json={})
        r1 = client.get("/papers/W1/card")
        etag = r1.headers.get("etag")
        assert etag

        r2 = client.get("/papers/W1/card", headers={"If-None-Match": etag})
        assert r2.status_code == 304

    def test_post_invalidates_cache(self, client, session, _patch_app_config, tmp_path, monkeypatch):
        from carrel.pipeline import paper_card as pipe_mod
        p = _make_paper(session, md_path="papers/W1/paper.md")
        _write_md(_patch_app_config, p, "# Intro\n\nLoRA body. " * 30)
        monkeypatch.setattr(pipe_mod.llm, "chat_json", _fake_llm({"method_name": "v1"}))

        client.post("/papers/W1/card/extract", json={})
        r1 = client.get("/papers/W1/card")
        first_etag = r1.headers.get("etag")

        monkeypatch.setattr(pipe_mod.llm, "chat_json", _fake_llm({"method_name": "v2"}))
        client.post("/papers/W1/card/extract", json={"force": True})
        r2 = client.get("/papers/W1/card")
        assert r2.json()["method_name"] == "v2"
        assert r2.headers.get("etag") != first_etag

    def test_post_returns_422_when_no_markdown(self, client, session, _patch_app_config):
        _make_paper(session)
        r = client.post("/papers/W1/card/extract", json={})
        assert r.status_code == 422
        assert "no md_path" in r.json()["detail"]

    def test_post_404_for_unknown_paper(self, client, _patch_app_config):
        r = client.post("/papers/missing/card/extract", json={})
        assert r.status_code == 404


def test_paper_model_has_card_columns():
    """Regression: the card columns must exist on the papers table."""
    from sqlalchemy import inspect

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("papers")}
    assert "paper_card" in cols
    assert "paper_card_extracted_at" in cols

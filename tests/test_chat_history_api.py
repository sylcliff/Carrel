"""Tests for the server-persisted per-paper chat transcript."""
from __future__ import annotations

from datetime import UTC, datetime

from carrel.models import ChatMessage, Paper


def _paper(pid: str) -> Paper:
    return Paper(
        id=pid,
        id_kind="openalex",
        title=f"Paper {pid}",
        venue="Nature",
        status="ready",
        oa_status="oa",
        source="openalex",
        in_library=True,
        discarded=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        authors=[{"name": "Jane Smith"}],
    )


def test_get_empty_transcript(client, session):
    session.add(_paper("W1"))
    session.commit()

    r = client.get("/papers/W1/chat/messages")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paper_id"] == "W1"
    assert body["messages"] == []
    assert body["updated_at"] is None


def test_put_replaces_and_returns_transcript(client, session):
    session.add(_paper("W1"))
    session.commit()
    before = session.get(Paper, "W1").updated_at

    payload = {
        "messages": [
            {"role": "user", "content": "What is X?"},
            {"role": "assistant", "content": "X is $x$."},
        ]
    }
    r = client.put("/papers/W1/chat/messages", json=payload)
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "What is X?"
    ids = [m["id"] for m in msgs]
    assert len(ids) == 2 and all(isinstance(i, int) for i in ids)

    # Persisted and ordered by id.
    session.expire_all()
    rows = session.query(ChatMessage).filter_by(paper_id="W1").order_by(ChatMessage.id).all()
    assert [(r.role, r.content) for r in rows] == [
        ("user", "What is X?"),
        ("assistant", "X is $x$."),
    ]
    assert session.get(Paper, "W1").updated_at >= before


def test_put_is_replace_not_append(client, session):
    session.add(_paper("W1"))
    session.commit()
    client.put(
        "/papers/W1/chat/messages",
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    r = client.put(
        "/papers/W1/chat/messages",
        json={"messages": [{"role": "assistant", "content": "second"}]},
    )
    assert r.status_code == 200
    contents = [m["content"] for m in r.json()["messages"]]
    assert contents == ["second"]


def test_put_drops_invalid_and_blank_turns(client, session):
    session.add(_paper("W1"))
    session.commit()
    r = client.put(
        "/papers/W1/chat/messages",
        json={
            "messages": [
                {"role": "system", "content": "ignored"},
                {"role": "user", "content": "   "},
                {"role": "user", "content": "keep me"},
                {"role": "assistant", "content": "  answer  "},
            ]
        },
    )
    assert r.status_code == 200
    kept = [(m["role"], m["content"]) for m in r.json()["messages"]]
    assert kept == [("user", "keep me"), ("assistant", "answer")]


def test_clear_with_empty_list(client, session):
    session.add(_paper("W1"))
    session.commit()
    client.put(
        "/papers/W1/chat/messages",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    r = client.put("/papers/W1/chat/messages", json={"messages": []})
    assert r.status_code == 200
    assert r.json()["messages"] == []
    session.expire_all()
    assert session.query(ChatMessage).filter_by(paper_id="W1").count() == 0


def test_missing_paper_returns_404(client):
    assert client.get("/papers/nope/chat/messages").status_code == 404
    assert (
        client.put(
            "/papers/nope/chat/messages", json={"messages": []}
        ).status_code
        == 404
    )


def test_put_rejects_too_many_turns(client, session):
    session.add(_paper("W1"))
    session.commit()
    too_many = [{"role": "user", "content": "x"}] * 501
    r = client.put("/papers/W1/chat/messages", json={"messages": too_many})
    assert r.status_code == 413

"""Subscription CRUD (M2; stubs in M1)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from carrel.db import get_session_dep
from carrel.models import Subscription
from carrel.schemas import SubscriptionIn, SubscriptionOut

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(session: Session = Depends(get_session_dep)) -> list[SubscriptionOut]:
    rows = session.exec(select(Subscription).order_by(Subscription.id)).all()
    return [
        SubscriptionOut(
            id=r.id or 0,
            kind=r.kind,
            value=r.value,
            label=r.label,
            enabled=r.enabled,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("", response_model=SubscriptionOut)
def create_subscription(
    body: SubscriptionIn, session: Session = Depends(get_session_dep)
) -> SubscriptionOut:
    if body.kind not in ("keyword", "author", "venue", "arxiv_category"):
        raise HTTPException(status_code=400, detail=f"invalid kind: {body.kind}")
    row = Subscription(
        kind=body.kind,
        value=body.value.strip(),
        label=body.label,
        enabled=body.enabled,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return SubscriptionOut(
        id=row.id or 0,
        kind=row.kind,
        value=row.value,
        label=row.label,
        enabled=row.enabled,
        created_at=row.created_at,
    )


@router.delete("/{sub_id}")
def delete_subscription(sub_id: int, session: Session = Depends(get_session_dep)) -> dict[str, bool]:
    row = session.get(Subscription, sub_id)
    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    session.delete(row)
    session.commit()
    return {"deleted": True}

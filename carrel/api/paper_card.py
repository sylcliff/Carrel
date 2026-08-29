"""Paper card API: read + extract the LLM-generated structured card.

Two routes, both single-paper:

* ``GET /papers/{id}/card`` — return the stored card (or 204 if not yet
  extracted).  Cached with an ETag keyed on ``paper_card_extracted_at`` so
  a re-extract round-trips invalidate it.
* ``POST /papers/{id}/card/extract`` — run the LLM extraction inline.
  Synchronous on purpose: the cost is one call (5-15s) per click.  A batch
  endpoint can wrap a Job driver later if a real need shows up.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session

from carrel.api._app_cache import cached, get_cache
from carrel.api._http_cache import (
    apply_etag_headers,
    etag_for_updated_at,
    if_none_match_matches,
)
from carrel.api._invalidation import invalidate_paper_mutated
from carrel.db import get_session_dep
from carrel.models import Paper
from carrel.pipeline.paper_card import PaperCardError, extract_paper_card
from carrel.schemas import PaperCardExtractRequest, PaperCardOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["paper_card"])


# ---------- GET /papers/{id}/card -------------------------------------------


@cached("paper_card", key_params=("paper_id",), tags=("paper", "paper:card"))
def _get_card_body(paper_id: str, session: Session) -> dict | None:
    """Cached lookup of one paper's structured card.  Returns ``None``
    when the paper doesn't exist or hasn't been extracted yet — the
    handler turns that into a 404 / 204 respectively so the cache key
    doesn't need a separate sentinel."""
    paper = session.get(Paper, paper_id)
    if paper is None or paper.paper_card is None:
        return None
    return paper.paper_card


@router.get("/papers/{paper_id}/card", response_model=PaperCardOut)
def get_paper_card(
    paper_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session_dep),
) -> Response:
    """Return the structured card for one paper.

    * 404 when the paper is unknown.
    * 204 No Content when the paper exists but has no card yet (caller
      should render the empty state and offer an "Extract" button).
    * 200 + ETag when the card is present.  The ETag is derived from
      ``paper_card_extracted_at`` so a re-extract invalidates it.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if paper.paper_card is None:
        # No card yet: tell the client not to cache this and let it
        # render the empty / "extract" state.
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    card = _get_card_body(paper_id, session)
    if card is None:
        # Race: paper deleted between checks.  404 is the honest answer.
        raise HTTPException(status_code=404, detail="paper not found")
    etag = etag_for_updated_at(paper.paper_card_extracted_at, extra=(paper.id,))
    if etag is not None and if_none_match_matches(request, etag):
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=60, stale-while-revalidate=120",
            },
        )
    if etag is not None:
        apply_etag_headers(response, etag, max_age=60, stale_while_revalidate=120)
    # Pydantic re-validates the JSON dict on the way out; a bad legacy
    # row would surface as a 500 here, which is the right signal to
    # re-extract.
    body = PaperCardOut.model_validate(card).model_dump_json()
    return Response(
        content=body,
        media_type="application/json",
        headers={k: v for k, v in response.headers.items()},
    )


# ---------- POST /papers/{id}/card/extract ----------------------------------


@router.post(
    "/papers/{paper_id}/card/extract",
    response_model=PaperCardOut,
)
def extract_paper_card_endpoint(
    paper_id: str,
    body: PaperCardExtractRequest | None = None,
    session: Session = Depends(get_session_dep),
) -> PaperCardOut:
    """Run the LLM extraction and return the new card.

    Synchronous: 5-15s blocking.  The route is single-paper on purpose;
    there's no batch endpoint yet.  ``force=true`` re-runs even when the
    paper has a fresh card.
    """
    from carrel.main import app_config

    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    force = bool(body and body.force)
    try:
        extract_paper_card(session, app_config, paper_id, force=force)
    except PaperCardError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    # Drop the cached body so the next GET hits the DB and rebuilds.
    invalidate_paper_mutated(paper_id, mutate={"card"})
    session.refresh(paper)
    if paper.paper_card is None:
        # The extraction succeeded but produced an empty card (LLM
        # returned nothing parseable).  Surface that honestly.
        raise HTTPException(
            status_code=422,
            detail="extraction produced no parseable card; try again with force=true",
        )
    return PaperCardOut.model_validate(paper.paper_card)


# ---------------------------------------------------------------------------
# Convenience: re-export the cache helper so the tests can reset it
# (the cached() decorator registers a memoizing wrapper; L2 invalidation
# already drops it on write, but a forced reset helps when a test wants
# to bypass invalidation and re-hit the function body).
# ---------------------------------------------------------------------------
__all__ = ["router", "get_cache"]

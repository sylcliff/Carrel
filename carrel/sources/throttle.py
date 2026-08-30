"""Process-wide 429 / rate-limit latch — one source-agnostic module.

When an upstream API returns HTTP 429, the polite response is to wait for
the ``Retry-After`` window before retrying — but a Retry-After measured
in *tens of thousands of seconds* (OpenAlex budget reset at next UTC
midnight) means a single lookup blocks the whole pipeline for hours.
This module records the cooldown once in a process-wide latch and lets
every subsequent call within the window short-circuit instead of paying
the full retry-wait.

The same machinery is intended to back a future Semantic Scholar or
arXiv throttle by instantiating another ``Throttle("semantic_scholar",
...)`` here. Today only OpenAlex uses it.

Three primitives:

- :class:`Throttle` — the latch itself. Thread-safe, monotonic-clock.
- :class:`ThrottleAwareRetry` — a ``urllib3.util.Retry`` subclass that
  records the *full* Retry-After on the latch and returns the *capped*
  value to urllib3 (so an individual request still fails fast).
- :func:`throttle_aware` — decorator that returns a function's empty
  sentinel when the latch is open (defense-in-depth and the path that
  returns function-shaped results to callers).

Plus one batch helper (:func:`abort_if_throttled`) and one exception
class (:class:`ThrottledError`).
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


def _format_duration(seconds: float) -> str:
    """Format a cooldown duration as ``"rate-limited, resets in Nh Mm"`` /
    ``"rate-limited, resets in Mm"``. Sub-minute durations render as 1m
    (we never want the user to see "0m" while the latch is still active).
    """
    hours = int(seconds // 3600)
    if seconds < 60:
        minutes = 1
    else:
        minutes = int((seconds % 3600) // 60)
    if hours > 0:
        return f"rate-limited, resets in {hours}h {minutes}m"
    return f"rate-limited, resets in {minutes}m"


class ThrottledError(Exception):
    """Raised when a call is short-circuited because the source is rate-limited.

    Carries a human-readable ``message`` (e.g. ``"rate-limited, resets in
    4h 12m"``) suitable for surfacing in API ``warnings`` arrays.

    Subclasses :class:`Exception` (not :class:`BaseException`) so existing
    ``except Exception`` blocks in source functions translate it into the
    function's empty-sentinel return value automatically. This is a
    deliberate API choice: the caller does not need to know that throttling
    is the cause of the empty result.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Throttle:
    """Process-wide 429 / rate-limit latch for one upstream source.

    Thread-safe: all state is guarded by a single lock. Monotonic clock:
    ``_until`` is measured in :func:`time.monotonic` seconds so a
    wall-clock NTP step does not skew the cooldown end.

    The latch is *monotonic*: a new :meth:`record` call cannot shorten an
    existing longer cooldown. This is what makes the latch safe under
    contention — 20 fan-out threads each recording a 429 will all observe
    the longest one.
    """

    def __init__(
        self,
        name: str,
        *,
        default_cooldown: float = 300.0,
        max_cooldown: float = 86400.0,
    ) -> None:
        """``default_cooldown`` is applied when a 429 has no Retry-After
        header (some OpenAlex 429s omit it). ``max_cooldown`` clamps a
        malformed or extreme Retry-After so a single bad response cannot
        lock the process out of a source for a week.
        """
        self.name = name
        self._default_cooldown = float(default_cooldown)
        self._max_cooldown = float(max_cooldown)
        self._lock = threading.Lock()
        self._until: float = 0.0  # monotonic seconds
        self._message: str = ""

    def is_open(self) -> bool:
        """True iff a recorded cooldown is still in effect."""
        with self._lock:
            return self._until > time.monotonic()

    def message(self) -> str:
        """The human-readable message associated with the current latch, or ``""``."""
        with self._lock:
            return self._message

    def record(self, retry_after: float | None) -> str:
        """Record a 429 hit. Returns the new human-readable message.

        ``retry_after`` is the *full* Retry-After in seconds (not any
        per-call sleep cap). When ``retry_after`` is None or non-positive,
        falls back to ``self._default_cooldown`` so a bare 429 still
        prevents the next caller from paying the full ~17 s of urllib3
        retries. Clamped to ``self._max_cooldown`` to keep a malformed
        header from locking us out indefinitely.

        The user-facing message is derived from the *recorded* duration,
        not the live remaining time. This avoids a per-call countdown
        (a 60 s record would otherwise read "1m" at T+0, "0m" at T+0.1,
        and still "0m" after the latch has expired) and gives the user a
        stable label for the whole cooldown window.
        """
        if retry_after is None or retry_after <= 0:
            retry_after = self._default_cooldown
        retry_after = min(float(retry_after), self._max_cooldown)
        with self._lock:
            new_until = time.monotonic() + retry_after
            if new_until > self._until:
                self._until = new_until
                self._message = _format_duration(retry_after)
        logger.warning("throttle[%s] recorded: %s", self.name, self._message)
        return self._message

    def clear(self) -> None:
        """Drop any pending cooldown. Called when a request succeeds and we
        have evidence the source's budget is restored (today: ``search_work``).
        """
        with self._lock:
            self._until = 0.0
            self._message = ""


# ---------------------------------------------------------------------------
# Singletons: one per source we throttle.
# Future sources (S2, arXiv, Crossref, …) get their own ``Throttle("name", …)``
# here.
# ---------------------------------------------------------------------------
openalex_throttle = Throttle(
    "openalex",
    default_cooldown=300.0,
    max_cooldown=86400.0,
)

crossref_throttle = Throttle(
    "crossref",
    default_cooldown=300.0,
    max_cooldown=86400.0,
)


# ---------------------------------------------------------------------------
# urllib3 Retry subclass: clamp long Retry-After AND record the full value.
# ---------------------------------------------------------------------------
class ThrottleAwareRetry:
    """``urllib3.util.Retry`` adapter that:

    * Clamps a long Retry-After to ``max_sleep_seconds`` (default 5 s) so a
      single ``cites:`` lookup does not block a job for ~10 minutes.
    * Records the *full* Retry-After on ``throttle`` so the *next* caller
      can short-circuit instead of repeating the same wait.

    When the 429 response carries no Retry-After header, records
    ``throttle._default_cooldown`` so a bare 429 still prevents the next
    caller from paying the full 5 s × N.

    Wired into pyalex by ``openalex_client.configure()`` — the single
    chokepoint that intercepts every pyalex call.
    """

    def __new__(cls, throttle: Throttle, max_sleep_seconds: float = 5.0, **kwargs: Any):
        # Late import: urllib3 is a pyalex dep, but importing it eagerly here
        # would couple the Throttle module to the network stack.
        from urllib3.util import Retry

        class _Inner(Retry):
            def get_retry_after(self, response: Any) -> float | None:  # type: ignore[override]
                val = super().get_retry_after(response)
                if val is None:
                    if response is not None and getattr(response, "status", None) == 429:
                        throttle.record(throttle._default_cooldown)
                    return val
                throttle.record(float(val))
                return min(float(val), max_sleep_seconds)

        return _Inner(**kwargs)


# ---------------------------------------------------------------------------
# Per-function decorator: defense-in-depth + return function-shaped results.
# ---------------------------------------------------------------------------
def throttle_aware(
    throttle: Throttle,
    *,
    sentinel: Any = None,
    clear_on_success: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that:

    * Checks ``throttle.is_open()`` on entry; returns ``sentinel`` if open
      so the caller sees the function's "empty" return value (``None`` for
      lookups, ``[]`` for fetch_* returning lists, etc.).
    * Catches :class:`ThrottledError` from the body and re-raises so the
      search endpoint's catch-block sees it.
    * On clean success, calls ``throttle.clear()`` iff
      ``clear_on_success=True`` (used by ``search_work`` only — it is the
      "canary" call that proves the daily budget is restored).
    * Lets other exceptions propagate unchanged.

    Why both a session wrapper (Layer 1) and this decorator (Layer 2)?
    Layer 1 raises :class:`ThrottledError`; the existing ``except
    Exception`` blocks in our public functions swallow it into the
    function's empty sentinel. But that means *every* error becomes the
    sentinel, which conflates "throttled" with "not in OpenAlex". The
    decorator adds a clean pre-check that returns the sentinel *only*
    when the latch is open, and otherwise lets the function's real error
    handling run.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if throttle.is_open():
                return cast(R, sentinel)
            result = fn(*args, **kwargs)
            if clear_on_success:
                throttle.clear()
            return result

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Batch-loop helper: cheap <1s shutdown for fan-out jobs.
# ---------------------------------------------------------------------------
def abort_if_throttled(
    throttle: Throttle,
    *,
    on_abort: Callable[[str], None] | None = None,
) -> str | None:
    """Convenience for batch loops. Returns the throttle message if the
    latch is open, ``None`` otherwise. If ``on_abort`` is given, it is
    called with the message (so the caller can mark remaining work as
    failed).

    Usage::

        msg = abort_if_throttled(
            openalex_throttle,
            on_abort=lambda m: _abort_remaining(session, job_ids, reason=f"throttled: {m}"),
        )
        if msg is not None:
            return
    """
    if throttle.is_open():
        msg = throttle.message()
        if on_abort is not None:
            on_abort(msg)
        return msg
    return None

"""Pure unit tests for :mod:`carrel.sources.throttle` — no network, no DB."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from carrel.sources.throttle import (
    Throttle,
    ThrottleAwareRetry,
    ThrottledError,
    abort_if_throttled,
    openalex_throttle,
    throttle_aware,
)


# ---------------------------------------------------------------------------
# Throttle class
# ---------------------------------------------------------------------------
def test_default_state_is_closed() -> None:
    t = Throttle("test")
    assert t.is_open() is False
    assert t.message() == ""


def test_record_then_is_open() -> None:
    t = Throttle("test")
    t.record(60.0)
    assert t.is_open() is True
    assert "rate-limited" in t.message()
    assert "resets in" in t.message()


def test_clear_resets() -> None:
    t = Throttle("test")
    t.record(60.0)
    t.clear()
    assert t.is_open() is False
    assert t.message() == ""


def test_record_none_uses_default() -> None:
    t = Throttle("test", default_cooldown=120.0)
    t.record(None)
    assert t.is_open() is True
    # Don't assert exact minutes — sub-second drift could round 119s to 1m.
    assert "rate-limited" in t.message()


def test_record_zero_uses_default() -> None:
    t = Throttle("test", default_cooldown=120.0)
    t.record(0)
    assert t.is_open() is True


def test_record_negative_uses_default() -> None:
    t = Throttle("test", default_cooldown=120.0)
    t.record(-1.0)
    assert t.is_open() is True


def test_record_clamps_to_max() -> None:
    t = Throttle("test", default_cooldown=300.0, max_cooldown=86400.0)
    # 10**9 seconds = ~31 years. Must clamp to 24h.
    t.record(10**9)
    # _until should be at most monotonic() + 86400, so message uses hours.
    msg = t.message()
    assert "24h" in msg or "23h" in msg  # allow up-to-1s slack


def test_record_is_monotonic() -> None:
    t = Throttle("test")
    t.record(1000.0)
    first_until = t._until  # noqa: SLF001 — test introspection
    t.record(100.0)  # shorter — must not shorten
    assert t._until == first_until  # noqa: SLF001
    # And the message still reflects the longer one
    assert "16m" in t.message() or "17m" in t.message()


def test_threading_safety() -> None:
    """20 threads × 1000 record/clear each: no exceptions, no torn state."""
    t = Throttle("test")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(1000):
                t.record(60.0)
                _ = t.is_open()
                _ = t.message()
                t.clear()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    # Final state: depends on race, but should be valid (either open with a
    # valid message, or closed with empty message).
    if t.is_open():
        assert t.message() != ""
    else:
        assert t.message() == ""


def test_message_uses_hours_when_long() -> None:
    t = Throttle("test", max_cooldown=86400.0)
    t.record(3700.0)  # 1h 1m
    msg = t.message()
    assert "1h" in msg
    assert "1m" in msg


def test_message_uses_minutes_when_short() -> None:
    t = Throttle("test")
    t.record(60.0)
    msg = t.message()
    assert "1m" in msg
    assert "h" not in msg


# ---------------------------------------------------------------------------
# ThrottledError
# ---------------------------------------------------------------------------
def test_carries_message() -> None:
    e = ThrottledError("foo")
    assert e.message == "foo"
    assert str(e) == "foo"


def test_subclasses_exception() -> None:
    assert issubclass(ThrottledError, Exception)
    # And NOT BaseException-only (must be catchable by `except Exception`)
    e = ThrottledError("x")
    try:
        raise e
    except Exception as caught:
        assert caught is e


# ---------------------------------------------------------------------------
# ThrottleAwareRetry
# ---------------------------------------------------------------------------
def _make_response(status: int, retry_after_header: str | None) -> MagicMock:
    """Build a urllib3-compatible response mock. urllib3 calls
    ``response.headers.get("Retry-After")`` and feeds the result to a regex
    matcher, so ``None`` for absent and a string for present — never a
    MagicMock.
    """
    resp = MagicMock()
    resp.status = status
    resp.headers = MagicMock()
    resp.headers.get = MagicMock(return_value=retry_after_header)
    return resp


def test_clamps_long_retry_after() -> None:
    t = Throttle("test", max_cooldown=86400.0)
    retry = ThrottleAwareRetry(t, max_sleep_seconds=5.0, total=3, status_forcelist=[429])
    resp = _make_response(429, "600")
    result = retry.get_retry_after(resp)
    # The retry sleep is capped at 5s
    assert result == 5.0
    # But the latch holds the full 600s
    assert t.is_open() is True
    assert "10m" in t.message()


def test_records_when_no_retry_after_on_429() -> None:
    t = Throttle("test", default_cooldown=300.0)
    retry = ThrottleAwareRetry(t, total=1, status_forcelist=[429])
    resp = _make_response(429, None)
    result = retry.get_retry_after(resp)
    assert result is None
    assert t.is_open() is True


def test_passes_through_2xx() -> None:
    t = Throttle("test")
    retry = ThrottleAwareRetry(t, total=3, status_forcelist=[429])
    resp = _make_response(200, None)
    result = retry.get_retry_after(resp)
    assert result is None
    assert t.is_open() is False


# ---------------------------------------------------------------------------
# throttle_aware decorator
# ---------------------------------------------------------------------------
def test_short_circuits_when_open() -> None:
    t = Throttle("test")
    t.record(60.0)

    called = []

    @throttle_aware(t, sentinel="EMPTY")
    def fn() -> str:
        called.append(1)
        return "real"

    assert fn() == "EMPTY"
    assert called == []


def test_passes_through_clean_success() -> None:
    t = Throttle("test")

    @throttle_aware(t)
    def fn() -> int:
        return 42

    assert fn() == 42
    assert t.is_open() is False


def test_clear_on_success_clears() -> None:
    """When the latch is just-expired (is_open() False but a stale message
    remains) a successful call with clear_on_success=True drops the message.
    """
    t = Throttle("test")
    t.record(60.0)
    # Fast-forward: pretend the latch has expired by reaching into _until.
    t._until = 0.0  # noqa: SLF001 — test introspection
    assert t.is_open() is False
    assert t.message() != ""  # stale message persists

    @throttle_aware(t, clear_on_success=True)
    def fn() -> int:
        return 42

    assert fn() == 42
    assert t.message() == ""  # clear() ran on success


def test_clear_on_success_default_false() -> None:
    """Without clear_on_success, a stale message survives the call."""
    t = Throttle("test")
    t.record(60.0)
    t._until = 0.0  # noqa: SLF001 — pretend the latch expired
    assert t.is_open() is False

    @throttle_aware(t)  # default: clear_on_success=False
    def fn() -> int:
        return 42

    assert fn() == 42
    assert t.message() != ""  # stale message preserved (no clear)


def test_propagates_other_exceptions() -> None:
    t = Throttle("test")

    @throttle_aware(t)
    def fn() -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        fn()
    assert t.is_open() is False


def test_sentinel_with_list() -> None:
    t = Throttle("test")
    t.record(60.0)

    @throttle_aware(t, sentinel=[])
    def fn() -> list:
        return [1, 2, 3]

    assert fn() == []


def test_preserves_function_metadata() -> None:
    t = Throttle("test")

    @throttle_aware(t)
    def my_function() -> None:
        """A docstring."""
        pass

    assert my_function.__name__ == "my_function"
    assert my_function.__doc__ == "A docstring."


# ---------------------------------------------------------------------------
# abort_if_throttled
# ---------------------------------------------------------------------------
def test_returns_none_when_closed() -> None:
    t = Throttle("test")
    called = []
    result = abort_if_throttled(t, on_abort=lambda m: called.append(m))
    assert result is None
    assert called == []


def test_returns_message_when_open() -> None:
    t = Throttle("test")
    t.record(120.0)
    called = []
    result = abort_if_throttled(t, on_abort=lambda m: called.append(m))
    assert result is not None
    # Avoid asserting exact minutes (sub-second drift can round 119s to 1m).
    assert "rate-limited" in result
    assert called == [result]


def test_on_abort_receives_same_message() -> None:
    t = Throttle("test")
    t.record(3700.0)
    received = []

    def on_abort(m: str) -> None:
        received.append(m)

    msg = abort_if_throttled(t, on_abort=on_abort)
    assert msg is not None
    assert received == [msg]


# ---------------------------------------------------------------------------
# Singleton sanity (so a typo in module load fails loudly)
# ---------------------------------------------------------------------------
def test_singleton_is_a_throttle() -> None:
    assert isinstance(openalex_throttle, Throttle)
    assert openalex_throttle.name == "openalex"

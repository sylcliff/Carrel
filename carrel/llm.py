"""LLM chat helper (M4).

Thin wrapper over ``litellm.completion`` that:
  - requests JSON output and parses it to a dict,
  - resolves the API key from env (reusing `embeddings._key_for`),
  - retries transient errors (429/5xx/timeout) with backoff,
  - optionally falls back to a second model when the primary has no key or
    raises a non-transient/retries-exhausted error,
  - imports litellm lazily so the module imports without keys installed.

This mirrors the shape of :mod:`carrel.embeddings`; the two modules share the
provider-prefix -> env-var map and the transient-error classifier.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Sequence
from typing import Any

from carrel import embeddings

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3


class LLMError(RuntimeError):
    """A chat completion could not be produced or parsed."""


def _configure_litellm() -> None:
    """Drop provider-unsupported params instead of raising (e.g. volcengine
    Doubao rejects ``response_format=json_object``).  Idempotent."""
    try:
        import litellm
    except ImportError:  # pragma: no cover - litellm is a hard dep
        return
    litellm.drop_params = True


_configure_litellm()


def has_key_for(model: str) -> bool:
    """True if an API key is configured (in env) for ``model``'s provider."""
    return embeddings._key_for(model) is not None


def chat_json(
    messages: Sequence[dict[str, str]],
    *,
    model: str,
    fallback_model: str | None = None,
    temperature: float = 0.2,
    timeout: int = 60,
    max_retries: int = DEFAULT_MAX_RETRIES,
    feature: str = "other",
    on_usage: "Any | None" = None,
) -> dict[str, Any]:
    """Call the chat model and return its content parsed as JSON.

    Tries ``model`` first; if that model has no configured key or the call
    fails after retries and a ``fallback_model`` is given, the fallback is
    tried once. Raises :class:`LLMError` if no model produces valid JSON.

    If ``on_usage`` is given it is called with the raw litellm response so
    the caller can persist the ``usage`` block (see :mod:`carrel.usage`).
    """
    attempts: list[tuple[str, str | None]] = []
    key = embeddings._key_for(model)
    if key:
        attempts.append((model, key))
    elif fallback_model:
        logger.info("no API key for primary model %r; using fallback", model)
    else:
        raise LLMError(
            f"No API key configured for {model!r}; set DEEPSEEK_API_KEY or "
            f"VOLCANO_API_KEY (see .env.example)"
        )

    fb_key = embeddings._key_for(fallback_model) if fallback_model else None
    if fallback_model and fb_key and (fallback_model, fb_key) not in attempts:
        attempts.append((fallback_model, fb_key))

    if not attempts:
        raise LLMError(
            f"No API key configured for {model!r}"
            + (f" or fallback {fallback_model!r}" if fallback_model else "")
            + "; set DEEPSEEK_API_KEY or VOLCANO_API_KEY (see .env.example)"
        )

    last_err: Exception | None = None
    for idx, (mdl, api_key) in enumerate(attempts):
        try:
            data, resp = _chat_with_retry(
                messages,
                model=mdl,
                api_key=api_key or "",
                temperature=temperature,
                timeout=timeout,
                max_retries=max_retries,
            )
        except LLMError:
            raise  # already a wrapped error
        except Exception as e:  # noqa: BLE001 - fall through to next model
            last_err = e
            logger.warning("chat_json with %s failed: %s", mdl, e)
            if idx < len(attempts) - 1:
                logger.info("trying fallback model %s", attempts[idx + 1][0])
            else:
                # Last attempt: normalize so callers only need to catch LLMError.
                raise LLMError(f"chat failed with {mdl}: {e}") from e
            continue
        # Notify the caller about the successful response so they can record
        # usage. Failures here must not break the result.
        if on_usage is not None:
            try:
                on_usage(mdl, feature, resp)
            except Exception as e:  # noqa: BLE001
                logger.warning("on_usage callback failed: %s", e)
        return data

    raise LLMError(f"all chat models failed; last error: {last_err}") from last_err


def _select_model(model: str, fallback_model: str | None) -> tuple[str, str]:
    """Pick (model, api_key) once before starting a stream.

    Unlike :func:`chat_json`, a stream can't swap models mid-response once the
    first token has been sent, so we resolve the key up front: use the primary
    if it has a key, else fall back. Raises :class:`LLMError` if neither does.
    """
    key = embeddings._key_for(model)
    if key:
        return model, key
    if fallback_model:
        fb_key = embeddings._key_for(fallback_model)
        if fb_key:
            logger.info("no API key for primary model %r; using fallback %s", model, fallback_model)
            return fallback_model, fb_key
    raise LLMError(
        f"No API key configured for {model!r}"
        + (f" or fallback {fallback_model!r}" if fallback_model else "")
        + "; set DEEPSEEK_API_KEY or VOLCANO_API_KEY (see .env.example)"
    )


def chat_stream(
    messages: Sequence[dict[str, str]],
    *,
    model: str,
    fallback_model: str | None = None,
    temperature: float = 0.3,
    timeout: int = 60,
    feature: str = "other",
    on_usage: Any | None = None,
    tools: list[dict[str, Any]] | None = None,
    on_tool_calls: "Any | None" = None,
) -> Iterator[str]:
    """Yield free-text completion deltas token-by-token (streaming, no JSON).

    The model/api key is selected before streaming begins. No retry is
    attempted once tokens are flowing — an error is raised so the caller can
    emit it on the stream.

    With ``stream_options={"include_usage": True}`` the final chunk carries
    a ``usage`` block; ``on_usage`` is invoked with that chunk so callers
    can record tokens (see :mod:`carrel.usage`). Fired eagerly on the
    first chunk that has a usage block so the record is not lost if the
    consumer breaks early.

    With ``tools=`` the model is offered the function-calling surface;
    tool-call deltas are accumulated by index and, after the stream ends,
    handed off as a list of OpenAI-shaped call dicts to ``on_tool_calls``
    (one invocation, after the last text delta is yielded). Callers
    without ``on_tool_calls`` see tool calls silently dropped — same as
    before this option existed, which keeps every existing call site
    working unchanged.
    """
    mdl, api_key = _select_model(model, fallback_model)

    from litellm import completion  # imported lazily so tests without keys work

    try:
        # Forward `tools` only when supplied; providers that don't support
        # function calling will have the param dropped (litellm.drop_params
        # is set globally at module load) so this is safe across providers.
        completion_kwargs: dict[str, Any] = dict(
            model=mdl,
            messages=list(messages),
            temperature=temperature,
            timeout=timeout,
            api_key=api_key,
            stream=True,
            stream_options={"include_usage": True},
        )
        if tools:
            completion_kwargs["tools"] = tools
        resp: Any = completion(**completion_kwargs)

        # Accumulator for tool-call deltas, keyed by the tool-call index
        # the provider assigns. Some providers emit only `id`, some only
        # `function.name`/`function.arguments` fragments — we concatenate
        # the string parts and remember whichever `id` shows up first.
        tool_accum: dict[int, dict[str, Any]] = {}
        for chunk in resp:
            # Some providers (e.g. DeepSeek) put the usage block on the
            # very last chunk; record it eagerly in case the consumer
            # breaks the loop before the generator finishes.
            if on_usage is not None and getattr(chunk, "usage", None) is not None:
                try:
                    on_usage(mdl, feature, chunk)
                except Exception as e:  # noqa: BLE001
                    logger.warning("on_usage callback failed: %s", e)
            try:
                delta = chunk.choices[0].delta
            except (AttributeError, IndexError, KeyError):
                delta = None
            # Text delta: yield as before.
            text = getattr(delta, "content", None) if delta is not None else None
            if text:
                yield text
            # Tool-call deltas: accumulate by index, no yields.
            tc_deltas = getattr(delta, "tool_calls", None) if delta is not None else None
            if tc_deltas:
                for tc in tc_deltas:
                    idx = getattr(tc, "index", None)
                    if idx is None:
                        # Single-call responses sometimes omit index.
                        idx = 0
                    entry = tool_accum.setdefault(idx, {
                        "id": None,
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if getattr(tc, "id", None):
                        entry["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            entry["function"]["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            entry["function"]["arguments"] += fn.arguments
    except Exception as e:  # noqa: BLE001 - normalized for the streaming caller
        raise LLMError(f"chat stream failed with {mdl}: {e}") from e

    # Stream finished — if the model returned tool calls, hand the
    # assembled list to the caller. Done after the loop so the caller
    # can be sure every text delta has already been yielded.
    if on_tool_calls is not None and tool_accum:
        calls: list[dict[str, Any]] = []
        for _idx in sorted(tool_accum):
            calls.append(tool_accum[_idx])
        try:
            on_tool_calls(calls)
        except Exception as e:  # noqa: BLE001
            # A buggy caller shouldn't crash the request; log and move on.
            logger.warning("on_tool_calls callback failed: %s", e)


def _chat_with_retry(
    messages: Sequence[dict[str, str]],
    *,
    model: str,
    api_key: str,
    temperature: float,
    timeout: int,
    max_retries: int,
) -> tuple[dict[str, Any], Any]:
    """One model's completion call with retry on transient errors.

    Returns ``(parsed_dict, raw_response)`` so callers can inspect
    ``raw_response.usage`` for token accounting.
    """
    from litellm import completion  # imported lazily so tests without keys work

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp: Any = completion(
                model=model,
                messages=list(messages),
                temperature=temperature,
                timeout=timeout,
                api_key=api_key,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            return _parse_json(content), resp
        except Exception as e:  # noqa: BLE001
            last_err = e
            transient = embeddings._is_transient(e)
            if not transient or attempt == max_retries:
                raise
            sleep_s = 2**attempt
            logger.warning(
                "chat call failed (attempt %d/%d), retrying in %ds: %s",
                attempt + 1, max_retries, sleep_s, e,
            )
            time.sleep(sleep_s)
    # Unreachable, but keeps the type checker happy.
    raise LLMError(f"chat failed after {max_retries} retries: {last_err}")


def _parse_json(content: Any) -> dict[str, Any]:
    """Parse a JSON-object string, tolerating fenced code blocks and stray text."""
    if content is None:
        raise LLMError("model returned empty content")
    if not isinstance(content, str):
        content = str(content)
    text = content.strip()

    # Some models wrap JSON in ```json ... ``` despite response_format.
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]  # drop opening fence
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]  # drop closing fence
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        # Last-ditch: grab the outermost {...} block.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                raise LLMError(f"model returned non-JSON content: {e}") from e
        else:
            raise LLMError(f"model returned non-JSON content: {e}") from e

    if not isinstance(data, dict):
        raise LLMError("model returned JSON that is not an object")
    return data

"""Timing and token stats for one streamed completion — the numbers behind a
direct-chat reply's "tok/s" badge and its hover popover.

Two sources are combined, and the provider wins wherever it reports:

- The provider's own accounting, read off the chunk that carries it. Native
  Ollama puts token counts and nanosecond durations on its final (`done`)
  chunk; llama.cpp servers attach a `timings` object; OpenAI-compatible
  servers send a choices-less `usage` chunk last when the request asked for
  `stream_options={"include_usage": True}`.
- What the client sees: when the first token arrived, when the last one did,
  and how many chunks carried text. Always available, so a server that
  reports nothing still gets a speed — computed from the chunk count, which
  local servers send one token at a time, and flagged `tokens_estimated`.

`total_ms` and `first_token_ms` are always client-measured: they are what
the operator waited, model load and network included.
"""

import time
from collections.abc import Callable
from typing import Any

from llm.activity import extract_usage

_NS_PER_MS = 1_000_000


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _per_second(count: float | None, ms: float | None) -> float | None:
    if not count or not ms or ms <= 0:
        return None
    return round(count * 1000.0 / ms, 1)


class GenerationStats:
    """Feed it every streamed chunk via `observe`, then read `result()`."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started_at = clock()
        self._first_token_at: float | None = None
        self._last_token_at: float | None = None
        self.chunks = 0
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self.prompt_ms: float | None = None
        self.decode_ms: float | None = None

    def observe(self, chunk: Any, produced_text: bool) -> None:
        """Record one chunk. `produced_text` — it carried a reasoning or
        answer delta — is what moves the first/last-token clock."""
        if produced_text:
            now = self._clock()
            if self._first_token_at is None:
                self._first_token_at = now
            self._last_token_at = now
            self.chunks += 1
        raw = getattr(chunk, "raw", None)
        if raw is None:
            return
        self._read_usage(raw)
        self._read_timings(_get(raw, "timings"))

    def _read_usage(self, raw: Any) -> None:
        # Ollama's mid-stream dicts carry no counts; only the done chunk does.
        if isinstance(raw, dict) and "prompt_eval_count" not in raw:
            return
        if not isinstance(raw, dict) and _get(raw, "usage") is None:
            return
        usage = extract_usage(raw)
        if usage["prompt_tokens"] is not None:
            self.prompt_tokens = usage["prompt_tokens"]
        if usage["completion_tokens"] is not None:
            self.completion_tokens = usage["completion_tokens"]
        if isinstance(raw, dict):
            # Exact nanoseconds, not extract_usage's whole milliseconds: a
            # 30 ms prefill rounds badly into a tokens-per-second figure.
            prefill_ns = _number(raw.get("prompt_eval_duration"))
            decode_ns = _number(raw.get("eval_duration"))
            if prefill_ns is not None:
                self.prompt_ms = prefill_ns / _NS_PER_MS
            if decode_ns is not None:
                self.decode_ms = decode_ns / _NS_PER_MS

    def _read_timings(self, timings: Any) -> None:
        """llama.cpp's per-request timings; only fills what usage left
        empty, since Ollama and `usage` are the documented sources."""
        if timings is None:
            return
        prompt_n = _number(_get(timings, "prompt_n"))
        predicted_n = _number(_get(timings, "predicted_n"))
        if self.prompt_tokens is None and prompt_n is not None:
            self.prompt_tokens = int(prompt_n)
        if self.completion_tokens is None and predicted_n is not None:
            self.completion_tokens = int(predicted_n)
        prompt_ms = _number(_get(timings, "prompt_ms"))
        predicted_ms = _number(_get(timings, "predicted_ms"))
        if prompt_ms is not None:
            self.prompt_ms = prompt_ms
        if predicted_ms is not None:
            self.decode_ms = predicted_ms

    def result(self) -> dict[str, Any]:
        """The stats as stored on the reply row's meta["generation"]. Every
        field but total_ms and chunks may be None."""
        now = self._clock()
        first_token_ms = None
        client_decode_ms = None
        if self._first_token_at is not None and self._last_token_at is not None:
            first_token_ms = (self._first_token_at - self._started_at) * 1000.0
            client_decode_ms = (self._last_token_at - self._first_token_at) * 1000.0
        generation_ms = self.decode_ms if self.decode_ms is not None \
            else client_decode_ms
        tokens_estimated = self.completion_tokens is None
        tokens = self.chunks if tokens_estimated else self.completion_tokens

        def ms(value: float | None) -> int | None:
            return None if value is None else round(value)

        return {
            "prompt_tokens": self.prompt_tokens,
            "prompt_ms": ms(self.prompt_ms),
            "prompt_tokens_per_second": _per_second(
                self.prompt_tokens, self.prompt_ms),
            "completion_tokens": self.completion_tokens,
            "generation_ms": ms(generation_ms),
            "tokens_per_second": _per_second(tokens, generation_ms),
            "tokens_estimated": tokens_estimated,
            "first_token_ms": ms(first_token_ms),
            "total_ms": ms((now - self._started_at) * 1000.0),
            "chunks": self.chunks,
        }

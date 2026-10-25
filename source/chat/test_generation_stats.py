"""Unit tests for chat.generation_stats — provider accounting per response
shape, and the client-side fallback when a server reports nothing."""

from types import SimpleNamespace

from chat.generation_stats import GenerationStats


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _chunk(raw=None) -> SimpleNamespace:
    return SimpleNamespace(raw=raw)


def test_ollama_done_chunk_supplies_counts_and_durations():
    clock = _Clock()
    stats = GenerationStats(clock=clock)
    clock.now += 0.4
    stats.observe(_chunk({"message": {"content": "Hi"}, "done": False}), True)
    clock.now += 5.0
    stats.observe(_chunk({
        "message": {"content": ""}, "done": True,
        "prompt_eval_count": 39, "prompt_eval_duration": 397_500_000,
        "eval_count": 435, "eval_duration": 5_090_000_000,
    }), False)
    clock.now += 0.1
    r = stats.result()
    assert r["prompt_tokens"] == 39
    assert r["prompt_ms"] == 398
    assert r["prompt_tokens_per_second"] == 98.1
    assert r["completion_tokens"] == 435
    assert r["generation_ms"] == 5090
    assert r["tokens_per_second"] == 85.5
    assert r["tokens_estimated"] is False
    assert r["first_token_ms"] == 400
    assert r["total_ms"] == 5500
    assert r["chunks"] == 1


def test_openai_usage_chunk_supplies_counts_and_client_times_generation():
    clock = _Clock()
    stats = GenerationStats(clock=clock)
    clock.now += 0.2
    stats.observe(_chunk(SimpleNamespace(choices=[object()], usage=None)), True)
    clock.now += 2.0
    stats.observe(_chunk(SimpleNamespace(choices=[object()], usage=None)), True)
    stats.observe(_chunk(SimpleNamespace(choices=[], usage=SimpleNamespace(
        prompt_tokens=12, completion_tokens=120))), False)
    r = stats.result()
    assert r["prompt_tokens"] == 12
    assert r["prompt_ms"] is None
    assert r["prompt_tokens_per_second"] is None
    assert r["completion_tokens"] == 120
    assert r["generation_ms"] == 2000
    assert r["tokens_per_second"] == 60.0
    assert r["tokens_estimated"] is False


def test_llamacpp_timings_fill_what_usage_leaves_empty():
    stats = GenerationStats(clock=_Clock())
    stats.observe(_chunk(SimpleNamespace(
        choices=[], usage=None,
        timings={"prompt_n": 50, "prompt_ms": 250.0,
                 "predicted_n": 200, "predicted_ms": 4000.0})), True)
    r = stats.result()
    assert r["prompt_tokens"] == 50
    assert r["prompt_tokens_per_second"] == 200.0
    assert r["completion_tokens"] == 200
    assert r["generation_ms"] == 4000
    assert r["tokens_per_second"] == 50.0


def test_silent_server_falls_back_to_chunk_count_flagged_estimated():
    clock = _Clock()
    stats = GenerationStats(clock=clock)
    for _ in range(11):
        clock.now += 0.1
        stats.observe(_chunk(None), True)
    r = stats.result()
    assert r["completion_tokens"] is None
    assert r["chunks"] == 11
    assert r["generation_ms"] == 1000
    assert r["tokens_per_second"] == 11.0
    assert r["tokens_estimated"] is True


def test_chunks_without_text_do_not_move_the_token_clock():
    clock = _Clock()
    stats = GenerationStats(clock=clock)
    clock.now += 1.0
    stats.observe(_chunk(None), False)
    r = stats.result()
    assert r["first_token_ms"] is None
    assert r["tokens_per_second"] is None
    assert r["chunks"] == 0

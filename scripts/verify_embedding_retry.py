#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from google.genai import errors as genai_errors

from memex.extract import Observation, store_observations
from memex.scripts.embeddings import EmbeddingPipeline, GeminiProvider


class ScenarioFailure(RuntimeError):
    pass


@dataclass(slots=True)
class FakeEmbedding:
    values: list[float]


@dataclass(slots=True)
class FakeResponse:
    embeddings: list[FakeEmbedding]


class FakeModels:
    """Sync fake matching the SDK's `client.models.embed_content` shape.

    The async embed path uses `asyncio.to_thread` over this sync method —
    intentional to keep the SDK's aio client off the critical path (its
    loop-binding semantics break under our running-loop fallback).
    """

    def __init__(self, client: 'FakeClient'):
        self._client = client

    def embed_content(self, *, model: str, contents: Any, config: Any) -> FakeResponse:
        texts = normalize_contents(contents)
        task_type = getattr(config, 'task_type', None)
        self._client.calls.append(
            {
                'model': model,
                'texts': list(texts),
                'batch_size': len(texts),
                'task_type': task_type,
            }
        )

        if self._client.failures_remaining > 0:
            self._client.failures_remaining -= 1
            raise rate_limit_error()

        return FakeResponse([FakeEmbedding(make_vector(text)) for text in texts])


class FakeClient:
    def __init__(self, failures_remaining: int = 0):
        self.failures_remaining = failures_remaining
        self.calls: list[dict[str, Any]] = []
        self.models = FakeModels(self)


class SleepRecorder:
    def __init__(self):
        self.calls: list[float] = []
        self.total = 0.0

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        self.total += float(seconds)


def normalize_contents(contents: Any) -> list[str]:
    if isinstance(contents, str):
        return [contents]
    if isinstance(contents, list):
        return [str(item) for item in contents]
    return [str(contents)]


def make_vector(text: str) -> list[float]:
    """Return a deterministic unit-norm vector derived from `text`.

    The provider asserts unit norm (see `_assert_unit_norm` in
    embeddings.py); fake vectors used here must satisfy that invariant
    or the retry scenarios will mis-fail on the assertion instead of
    exercising the path we actually want to test.
    """
    import math

    seed = float((sum(ord(ch) for ch in text) % 1000) + 1)
    raw = [seed / 1000.0, seed / 2000.0, seed / 3000.0, seed / 4000.0]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def rate_limit_error() -> genai_errors.ClientError:
    return genai_errors.ClientError(
        429,
        {
            'error': {
                'code': 429,
                'status': 'RESOURCE_EXHAUSTED',
                'message': 'Synthetic rate limit for verification',
            }
        },
    )


def build_pipeline() -> EmbeddingPipeline:
    os.environ['GEMINI_API_KEY'] = 'fake-key'
    pipeline = EmbeddingPipeline(
        config={
            'provider': 'google',
            'model': 'gemini-embedding-2',
            'dimensions': 4,
            'api_key_env': 'GEMINI_API_KEY',
        }
    )
    if not pipeline.enabled or not isinstance(pipeline._provider_impl, GeminiProvider):
        raise ScenarioFailure('EmbeddingPipeline did not initialize a GeminiProvider')
    return pipeline


def make_observations(count: int) -> list[Observation]:
    return [
        Observation(
            content=f'Observation {i}: the memex embedding retry path should stay testable.',
            obs_type='explicit',
            confidence='high',
        )
        for i in range(count)
    ]


@contextmanager
def patched_runtime(fake_client: FakeClient, sleep_recorder: SleepRecorder):
    """Patch `_get_client` to return the fake and `asyncio.sleep` to record
    backoff durations without actually sleeping. The embed path now awaits
    `asyncio.sleep` for retry backoff (was `time.sleep`).

    Also seeds `random` so the ±20% jitter on `GEMINI_BACKOFF_SCHEDULE` is
    deterministic — without this, sleep assertions become flaky.
    """
    original_get_client = GeminiProvider._get_client
    original_async_sleep = asyncio.sleep

    def fake_get_client(self: GeminiProvider) -> FakeClient:
        return fake_client

    async def fake_async_sleep(seconds: float) -> None:
        sleep_recorder(seconds)

    GeminiProvider._get_client = fake_get_client
    asyncio.sleep = fake_async_sleep
    random.seed(0xC0DE)
    try:
        yield
    finally:
        GeminiProvider._get_client = original_get_client
        asyncio.sleep = original_async_sleep


def scenario_clean_batch() -> tuple[bool, str]:
    fake_client = FakeClient()
    sleep_recorder = SleepRecorder()
    pipeline = build_pipeline()
    observations = make_observations(15)

    with tempfile.TemporaryDirectory(prefix='memex-verify-') as tmpdir:
        index_path = Path(tmpdir) / 'index.sqlite'
        with patched_runtime(fake_client, sleep_recorder):
            inserted = store_observations(index_path, 'verify/scenario1.md', observations, pipeline)

    batch_sizes = [call['batch_size'] for call in fake_client.calls]
    # store_observations returns {"inserted", "embedded", "embed_failed"} as of v0.11.0
    inserted_count = inserted.get("inserted") if isinstance(inserted, dict) else inserted
    ok = inserted_count == 15 and len(fake_client.calls) == 1 and batch_sizes == [15]
    diagnosis = f'inserted={inserted}, calls={len(fake_client.calls)}, batch_sizes={batch_sizes}'
    return ok, diagnosis


def _in_jitter_range(actual: float, base: float, tolerance: float = 0.21) -> bool:
    """Backoff sleeps are jittered ±20% to prevent thundering-herd retries.
    Tests seed `random` so values are deterministic; the predicate stays a
    range to keep the harness robust to small jitter-policy tweaks (and to
    a 1% safety margin on the 20% window).
    """
    return base * (1 - tolerance) <= actual <= base * (1 + tolerance)


def scenario_retry_then_success() -> tuple[bool, str]:
    fake_client = FakeClient(failures_remaining=2)
    sleep_recorder = SleepRecorder()
    provider = build_pipeline()._provider_impl
    assert isinstance(provider, GeminiProvider)
    texts = [f'retry scenario text {i}' for i in range(15)]

    with patched_runtime(fake_client, sleep_recorder):
        result = provider.embed_texts(texts, task_type='document')

    non_null = sum(1 for item in result if item is not None)
    batch_sizes = [call['batch_size'] for call in fake_client.calls]
    sleeps_in_range = (
        len(sleep_recorder.calls) == 2
        and _in_jitter_range(sleep_recorder.calls[0], 10.0)
        and _in_jitter_range(sleep_recorder.calls[1], 30.0)
    )
    ok = (
        len(fake_client.calls) == 3
        and batch_sizes == [15, 15, 15]
        and non_null == 15
        and sleeps_in_range
    )
    diagnosis = (
        f'calls={len(fake_client.calls)}, batch_sizes={batch_sizes}, '
        f'vectors={non_null}, sleep_calls={[round(s, 2) for s in sleep_recorder.calls]}, '
        f'sleeps_in_jitter_range={sleeps_in_range}'
    )
    return ok, diagnosis


def scenario_persistent_429() -> tuple[bool, str]:
    fake_client = FakeClient(failures_remaining=10)
    sleep_recorder = SleepRecorder()
    provider = build_pipeline()._provider_impl
    assert isinstance(provider, GeminiProvider)
    texts = [f'persistent retry text {i}' for i in range(15)]

    caught: Exception | None = None
    partial_results = None
    returned = None

    with patched_runtime(fake_client, sleep_recorder):
        try:
            returned = provider.embed_texts(texts, task_type='document')
        except Exception as exc:
            caught = exc
            partial_results = getattr(exc, 'results', None)

    batch_sizes = [call['batch_size'] for call in fake_client.calls]
    sleeps_in_range = (
        len(sleep_recorder.calls) == 3
        and _in_jitter_range(sleep_recorder.calls[0], 10.0)
        and _in_jitter_range(sleep_recorder.calls[1], 30.0)
        and _in_jitter_range(sleep_recorder.calls[2], 90.0)
    )
    ok = (
        caught is not None
        and len(fake_client.calls) == 4
        and batch_sizes == [15, 15, 15, 15]
        and sleeps_in_range
        and isinstance(partial_results, list)
        and len(partial_results) == 15
        and all(item is None for item in partial_results)
    )
    diagnosis = (
        f'raised={type(caught).__name__ if caught else None}, '
        f'calls={len(fake_client.calls)}, batch_sizes={batch_sizes}, '
        f'sleep_calls={[round(s, 2) for s in sleep_recorder.calls]}, '
        f'sleeps_in_jitter_range={sleeps_in_range}, '
        f'returned_is_none={returned is None}, '
        f'partial_len={len(partial_results) if isinstance(partial_results, list) else None}'
    )
    return ok, diagnosis


def scenario_concurrent_multi_batch() -> tuple[bool, str]:
    """Three sub-batches dispatched concurrently under GEMINI_CONCURRENCY.

    Verifies the async/concurrent path (added in v0.11.2): multiple
    sub-batches run in parallel, no inter-batch sleep is added, and the
    final result is reassembled in submission order regardless of task
    completion order. Forces batching deterministically by monkey-patching
    the token-budget batcher.
    """
    fake_client = FakeClient()
    sleep_recorder = SleepRecorder()
    pipeline = build_pipeline()
    provider = pipeline._provider_impl
    assert isinstance(provider, GeminiProvider)

    texts = [f'concurrent batch text {i}' for i in range(15)]
    original_batcher = provider._batch_texts_by_token_budget
    provider._batch_texts_by_token_budget = lambda ts: [ts[0:5], ts[5:10], ts[10:15]]

    try:
        with patched_runtime(fake_client, sleep_recorder):
            result = provider.embed_texts(texts, task_type='document')
    finally:
        provider._batch_texts_by_token_budget = original_batcher

    non_null = sum(1 for item in result if item is not None)
    batch_sizes = sorted(call['batch_size'] for call in fake_client.calls)

    # Submission-order preservation: result[i] must derive from texts[i],
    # even if the batch containing it completed last.
    order_preserved = (
        len(result) == 15
        and result[0] == make_vector(texts[0])
        and result[7] == make_vector(texts[7])
        and result[-1] == make_vector(texts[-1])
    )

    ok = (
        len(fake_client.calls) == 3
        and batch_sizes == [5, 5, 5]
        and non_null == 15
        and len(sleep_recorder.calls) == 0
        and order_preserved
    )
    diagnosis = (
        f'calls={len(fake_client.calls)}, batch_sizes={batch_sizes}, '
        f'vectors={non_null}, sleeps={sleep_recorder.calls}, '
        f'order_preserved={order_preserved}'
    )
    return ok, diagnosis


def scenario_inside_running_loop() -> tuple[bool, str]:
    """Verify the sync `embed_texts` API works when the caller is already
    inside an event loop (as `scripts/mcp_server.py` async tool handlers
    are). Without the running-loop fallback, `asyncio.run` raises
    `RuntimeError: asyncio.run() cannot be called from a running event loop`.
    """
    fake_client = FakeClient()
    sleep_recorder = SleepRecorder()
    pipeline = build_pipeline()
    provider = pipeline._provider_impl
    assert isinstance(provider, GeminiProvider)

    texts = [f'inside-loop text {i}' for i in range(5)]

    async def caller_inside_loop() -> list[Any]:
        # This is the regression we're guarding against: a coroutine on an
        # active event loop reaching the sync `embed_texts` shim.
        return provider.embed_texts(texts, task_type='document')

    raised: Exception | None = None
    result: list[Any] | None = None
    with patched_runtime(fake_client, sleep_recorder):
        try:
            result = asyncio.run(caller_inside_loop())
        except Exception as exc:
            raised = exc

    non_null = sum(1 for item in result or () if item is not None)
    ok = (
        raised is None
        and result is not None
        and non_null == 5
        and len(fake_client.calls) == 1
    )
    diagnosis = (
        f'raised={type(raised).__name__ if raised else None}, '
        f'vectors={non_null}, calls={len(fake_client.calls)}'
    )
    return ok, diagnosis


def main() -> int:
    scenarios = [
        ('clean batch of 15 observations', scenario_clean_batch),
        ('429 twice then succeed', scenario_retry_then_success),
        ('persistent 429', scenario_persistent_429),
        ('concurrent multi-batch (3 sub-batches)', scenario_concurrent_multi_batch),
        ('sync embed_texts inside running event loop', scenario_inside_running_loop),
    ]

    failures = 0
    for name, runner in scenarios:
        try:
            ok, diagnosis = runner()
        except Exception as exc:
            ok = False
            diagnosis = f'script error: {type(exc).__name__}: {exc}'

        status = 'PASS' if ok else 'FAIL'
        print(f'{status}: {name} :: {diagnosis}')
        if not ok:
            failures += 1

    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())

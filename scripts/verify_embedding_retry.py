#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import time
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
            'model': 'gemini-embedding-2-preview',
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
    original_get_client = GeminiProvider._get_client
    original_sleep = time.sleep

    def fake_get_client(self: GeminiProvider) -> FakeClient:
        return fake_client

    GeminiProvider._get_client = fake_get_client
    time.sleep = sleep_recorder
    try:
        yield
    finally:
        GeminiProvider._get_client = original_get_client
        time.sleep = original_sleep


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
    ok = (
        len(fake_client.calls) == 3
        and batch_sizes == [15, 15, 15]
        and non_null == 15
        and sleep_recorder.total == 40.0
        and sleep_recorder.calls == [10.0, 30.0]
    )
    diagnosis = (
        f'calls={len(fake_client.calls)}, batch_sizes={batch_sizes}, '
        f'vectors={non_null}, sleep_calls={sleep_recorder.calls}, sleep_total={sleep_recorder.total}'
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
    ok = (
        caught is not None
        and len(fake_client.calls) == 4
        and batch_sizes == [15, 15, 15, 15]
        and sleep_recorder.calls == [10.0, 30.0, 90.0]
        and sleep_recorder.total == 130.0
        and isinstance(partial_results, list)
        and len(partial_results) == 15
        and all(item is None for item in partial_results)
    )
    diagnosis = (
        f'raised={type(caught).__name__ if caught else None}, '
        f'calls={len(fake_client.calls)}, batch_sizes={batch_sizes}, '
        f'sleep_calls={sleep_recorder.calls}, sleep_total={sleep_recorder.total}, '
        f'returned_is_none={returned is None}, '
        f'partial_len={len(partial_results) if isinstance(partial_results, list) else None}'
    )
    return ok, diagnosis


def main() -> int:
    scenarios = [
        ('clean batch of 15 observations', scenario_clean_batch),
        ('429 twice then succeed', scenario_retry_then_success),
        ('persistent 429', scenario_persistent_429),
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

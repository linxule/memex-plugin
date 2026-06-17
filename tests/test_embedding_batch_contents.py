"""Regression: batch embedding must wrap each text in its own Content.

The Gemini SDK interprets `embed_content(contents=[str, str, ...])` (a list of
bare strings) as the *parts of a single Content* and returns ONE embedding for
the whole list — the rest silently None (batch under-population). Passing a list
of `types.Content`, one per text, returns one embedding per text. This pins the
fix so a future refactor can't regress to bare strings.

No real API: a fake client captures the `contents` argument.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from google.genai import types  # noqa: E402

from memex.scripts import embeddings as emb  # noqa: E402


class _FakeEmbedding:
    def __init__(self, values):
        self.values = values


class _FakeResponse:
    def __init__(self, n):
        # Unit-norm 4d vectors so _assert_unit_norm passes.
        self.embeddings = [_FakeEmbedding([1.0, 0.0, 0.0, 0.0]) for _ in range(n)]


class _FakeModels:
    def __init__(self, sink):
        self._sink = sink

    def embed_content(self, model, contents, config):
        self._sink["contents"] = contents
        # Mirror the SDK contract: one embedding per Content in the list.
        return _FakeResponse(len(contents))


class _FakeClient:
    def __init__(self, sink):
        self.models = _FakeModels(sink)


def _provider():
    os.environ.setdefault("MEMEX_TEST_KEY", "fake-key-not-used")
    cfg = {
        "provider": "google",
        "model": "gemini-embedding-2",
        "dimensions": 4,
        "api_key_env": "MEMEX_TEST_KEY",
    }
    return emb.GeminiProvider(cfg)


def test_embed_texts_wraps_each_text_as_its_own_content():
    sink: dict = {}
    p = _provider()
    p._client = _FakeClient(sink)

    out = p.embed_texts(["alpha", "beta", "gamma"], task_type="document")

    # One embedding per input text (not 1-of-N).
    assert len([v for v in out if v]) == 3

    # The SDK must have received Content objects, one per text — NOT bare
    # strings (which would collapse to a single embedding).
    contents = sink["contents"]
    assert len(contents) == 3
    assert all(isinstance(c, types.Content) for c in contents)
    assert [c.parts[0].text for c in contents] == ["alpha", "beta", "gamma"]


def test_single_text_still_one_content():
    sink: dict = {}
    p = _provider()
    p._client = _FakeClient(sink)
    out = p.embed_texts(["solo"], task_type="document")
    assert len([v for v in out if v]) == 1
    assert [c.parts[0].text for c in sink["contents"]] == ["solo"]

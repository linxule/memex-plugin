"""Tests for the unit-norm invariant on embedding provider output.

sqlite-vec uses L2 distance by default; for cosine-correct ranking the
stored vectors must be unit-norm. `_assert_unit_norm` is the guard that
catches provider drift at write time instead of silently degrading
search quality.
"""
from __future__ import annotations

import math

import pytest

from memex.scripts.embeddings import _assert_unit_norm


def _unit_vec(dim: int = 8) -> list[float]:
    """Build a small unit-norm vector (deterministic, cheap)."""
    raw = [float(i + 1) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def test_assert_unit_norm_accepts_normalized_vector():
    batch = [_unit_vec()]
    _assert_unit_norm(batch, provider="gemini")  # no exception


def test_assert_unit_norm_accepts_batch_with_nones():
    # None entries represent items the provider failed on — should be skipped.
    batch = [None, _unit_vec(), None]
    _assert_unit_norm(batch, provider="gemini")


def test_assert_unit_norm_accepts_empty_batch():
    _assert_unit_norm([], provider="gemini")
    _assert_unit_norm([None], provider="gemini")


def test_assert_unit_norm_accepts_tolerance_slack():
    """Float drift: a vector very close to unit norm should pass."""
    vec = _unit_vec()
    # Scale by 1.005 — still well inside the 2% tolerance window (~||v||^2≈1.01)
    vec = [x * 1.005 for x in vec]
    _assert_unit_norm([vec], provider="gemini")


def test_assert_unit_norm_rejects_doubled_vector():
    """A vector with magnitude 2 → ||v||^2 = 4, well outside tolerance."""
    vec = [x * 2 for x in _unit_vec()]
    with pytest.raises(ValueError, match="non-unit vector"):
        _assert_unit_norm([vec], provider="gemini")


def test_assert_unit_norm_rejects_halved_vector():
    """A vector with magnitude 0.5 → ||v||^2 = 0.25, outside tolerance."""
    vec = [x * 0.5 for x in _unit_vec()]
    with pytest.raises(ValueError, match="non-unit vector"):
        _assert_unit_norm([vec], provider="lmstudio")


def test_assert_unit_norm_error_names_provider():
    vec = [x * 3 for x in _unit_vec()]
    with pytest.raises(ValueError, match="custom-provider"):
        _assert_unit_norm([vec], provider="custom-provider")


def test_assert_unit_norm_checks_last_item_too():
    """The helper samples first and last — ensure a bad vector at the
    tail of the batch is caught (not just the head)."""
    good = _unit_vec()
    bad = [x * 4 for x in good]
    with pytest.raises(ValueError, match="non-unit vector"):
        _assert_unit_norm([good, good, good, bad], provider="gemini")

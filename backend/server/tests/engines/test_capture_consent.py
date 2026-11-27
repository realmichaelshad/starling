"""Which metric captures need passive-learning consent (CONTRACTS 6.1)."""
from __future__ import annotations

from starling_server.api.engine_routes import _is_passive_metrics


def test_publication_proofs_need_only_the_main_disclosure():
    assert _is_passive_metrics({"metadata": {"capture_mode": "publication_proof"}}) is False


def test_everything_else_counts_as_passive():
    assert _is_passive_metrics({"metadata": {"capture_mode": "passive_visible_post"}}) is True
    assert _is_passive_metrics({"metadata": {"capture_mode": "passive_visible_page"}}) is True
    assert _is_passive_metrics({"metadata": {"capture_mode": 7}}) is True
    assert _is_passive_metrics({"metadata": {}}) is True
    assert _is_passive_metrics({"metadata": "not an object"}) is True
    assert _is_passive_metrics({}) is True

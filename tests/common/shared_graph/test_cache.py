from __future__ import annotations

import os
import time

from checkov.common.shared_graph.builder import SharedGraphBuilder
from checkov.common.shared_graph.cache import GraphCache
from checkov.common.shared_graph.config import SharedGraphConfig
from checkov.common.shared_graph.adapters import DEFAULT_ADAPTERS
from checkov.common.shared_graph.mapping import LabelMapping


def _build(files: list[str]) -> object:
    mapping = LabelMapping.from_file(SharedGraphConfig.DEFAULT_MAPPING_FILE)
    return SharedGraphBuilder(SharedGraphConfig(enabled=True), mapping, DEFAULT_ADAPTERS).build(files).graph


def test_cache_round_trip(sample_repo, tmp_path) -> None:
    files = [
        str(sample_repo / "bucket.yaml"),
        str(sample_repo / "generic.yaml"),
        str(sample_repo / "shared" / "policy.yaml"),
    ]
    graph = _build(files)
    cache = GraphCache(tmp_path / "graph.json")
    cache.store(graph)

    restored = cache.load(files)

    assert restored is not None
    assert restored.nodes.keys() == graph.nodes.keys()
    assert restored.edges.keys() == graph.edges.keys()


def test_cache_invalidated_by_content_change(sample_repo, tmp_path) -> None:
    bucket = sample_repo / "bucket.yaml"
    files = [str(bucket)]
    cache = GraphCache(tmp_path / "graph.json")
    cache.store(_build(files))

    time.sleep(0.01)
    bucket.write_text(bucket.read_text() + "\n# changed\n")
    os.utime(bucket, None)

    assert cache.load(files) is None


def test_cache_invalidated_by_file_set_change(sample_repo, tmp_path) -> None:
    files = [str(sample_repo / "bucket.yaml")]
    cache = GraphCache(tmp_path / "graph.json")
    cache.store(_build(files))

    assert cache.load([*files, str(sample_repo / "generic.yaml")]) is None
    assert cache.load(files[:-1]) is None


def test_corrupt_cache_fails_closed(tmp_path) -> None:
    cache_file = tmp_path / "graph.json"
    cache_file.write_text("{not json")

    assert GraphCache(cache_file).load(["whatever.yaml"]) is None

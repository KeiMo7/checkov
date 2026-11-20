from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from checkov.common.shared_graph.file_parser import fingerprint_file, hash_file
from checkov.common.shared_graph.models import EdgeType, FileFingerprint, GraphEdge, GraphNode, NodeStatus, UnifiedResourceGraph

logger = logging.getLogger(__name__)


class GraphCache:
    """Disk-backed cache with cheap, fail-closed consistency checks.

    A cache entry is trusted only when the file's ``(mtime_ns, size)`` pair is
    unchanged. When the cheap check fails the content hash is compared as a
    tie breaker (some editors preserve size). Any mismatch invalidates the
    whole cache for that scan; the caller then rebuilds rather than risk a
    graph that disagrees with the current files.
    """

    SCHEMA_VERSION = 1

    def __init__(self, cache_file: str | Path, max_cached_files: int = 100_000) -> None:
        self.cache_file = Path(cache_file)
        self.max_cached_files = max_cached_files

    def store(self, graph: UnifiedResourceGraph) -> None:
        payload = {
            "schema": self.SCHEMA_VERSION,
            "max_nodes": graph.max_nodes,
            "nodes": [self._dump_node(node) for node in graph.nodes.values()],
            "edges": [self._dump_edge(edge) for edge in graph.edges.values()],
            "fingerprints": [asdict(fingerprint) for fingerprint in graph.fingerprints.values()],
        }
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.cache_file.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        tmp_path.replace(self.cache_file)

    def load(self, current_files: list[str]) -> UnifiedResourceGraph | None:
        if not self.cache_file.exists():
            return None
        try:
            with self.cache_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("schema") != self.SCHEMA_VERSION:
                logger.info("shared graph cache: schema mismatch, rebuilding")
                return None
            if not self._is_consistent(payload.get("fingerprints", []), current_files):
                logger.info("shared graph cache: inconsistent with current files, rebuilding")
                return None
            return self._restore(payload)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            logger.warning("shared graph cache: unusable (%s), rebuilding", error)
            return None

    def _is_consistent(self, cached: list[dict[str, Any]], current_files: list[str]) -> bool:
        cached_by_path = {entry["path"]: entry for entry in cached}
        if set(cached_by_path) != set(current_files):
            return False
        for path in current_files:
            entry = cached_by_path[path]
            current = fingerprint_file(path)
            stored = FileFingerprint(
                path=entry["path"],
                mtime_ns=entry["mtime_ns"],
                size=entry["size"],
                sha256=entry.get("sha256"),
            )
            if stored.cheap_matches(current):
                continue
            # Cheap metadata moved: trust the content hash, never the stale entry.
            if stored.sha256 is None or stored.sha256 != hash_file(path):
                return False
        return True

    def _restore(self, payload: dict[str, Any]) -> UnifiedResourceGraph:
        graph = UnifiedResourceGraph(max_nodes=payload.get("max_nodes"))
        for node_payload in payload["nodes"]:
            graph.add_node(
                GraphNode(
                    node_id=node_payload["node_id"],
                    canonical_type=node_payload["canonical_type"],
                    source_file=node_payload["source_file"],
                    attributes=node_payload["attributes"],
                    framework_types=node_payload["framework_types"],
                    status=NodeStatus(node_payload["status"]),
                    warnings=node_payload.get("warnings", []),
                    line_number=node_payload.get("line_number"),
                )
            )
        for edge_payload in payload["edges"]:
            graph.add_edge(
                GraphEdge(
                    source=edge_payload["source"],
                    target=edge_payload["target"],
                    edge_type=EdgeType(edge_payload["edge_type"]),
                    attribute=edge_payload.get("attribute"),
                )
            )
        for entry in payload["fingerprints"][: self.max_cached_files]:
            graph.fingerprints[entry["path"]] = FileFingerprint(**entry)
        return graph

    @staticmethod
    def _dump_node(node: GraphNode) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "canonical_type": node.canonical_type,
            "source_file": node.source_file,
            "attributes": node.attributes,
            "framework_types": node.framework_types,
            "status": node.status.value,
            "warnings": node.warnings,
            "line_number": node.line_number,
        }

    @staticmethod
    def _dump_edge(edge: GraphEdge) -> dict[str, Any]:
        return {
            "source": edge.source,
            "target": edge.target,
            "edge_type": edge.edge_type.value,
            "attribute": edge.attribute,
        }

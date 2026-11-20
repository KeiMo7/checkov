from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NodeStatus(str, Enum):
    """Lifecycle state of a node in the unified graph."""

    RESOLVED = "resolved"
    """The node was produced from a successfully parsed file."""

    PLACEHOLDER = "placeholder"
    """The node is referenced but its defining file/resource could not be parsed."""

    STALE = "stale"
    """The node has not been refreshed during the latest incremental update yet."""


class EdgeType(str, Enum):
    """The edge vocabulary understood by every framework adapter."""

    CONTAINS = "contains"
    """File -> resource containment edge."""

    REFERENCES = "references"
    """Resource -> resource cross-file reference edge."""

    DEPENDS_ON = "depends_on"
    """Explicit dependency (e.g. CloudFormation DependsOn)."""


@dataclass
class GraphNode:
    """A single node in the :class:`UnifiedResourceGraph`.

    ``resource_type`` holds the *canonical* (framework-neutral) label while
    ``framework_types`` keeps the original label of every framework that can
    see the node. Framework reports must restore their own label through
    :class:`~checkov.common.shared_graph.mapping.LabelMapping` instead of
    reading the canonical label directly.
    """

    node_id: str
    canonical_type: str
    source_file: str
    attributes: dict[str, Any] = field(default_factory=dict)
    framework_types: dict[str, str] = field(default_factory=dict)
    status: NodeStatus = NodeStatus.RESOLVED
    warnings: list[str] = field(default_factory=list)
    line_number: int | None = None

    def native_type(self, framework: str) -> str:
        """Return the framework-specific label, falling back to the canonical one."""

        return self.framework_types.get(framework, self.canonical_type)


@dataclass
class GraphEdge:
    """A directed edge between two node ids."""

    source: str
    target: str
    edge_type: EdgeType
    attribute: str | None = None

    def key(self) -> tuple[str, str, EdgeType, str | None]:
        return self.source, self.target, self.edge_type, self.attribute


@dataclass
class FileFingerprint:
    """Cheap consistency proof for a parsed file.

    The graph cache is trusted only when the pair ``(mtime_ns, size)`` matches
    and, when available, the content hash matches. Hashing is therefore only
    paid for files whose cheap metadata changed.
    """

    path: str
    mtime_ns: int
    size: int
    sha256: str | None = None

    def cheap_matches(self, other: FileFingerprint) -> bool:
        return self.path == other.path and self.mtime_ns == other.mtime_ns and self.size == other.size


class UnifiedResourceGraph:
    """Framework-neutral resource graph shared by every framework adapter.

    The graph deliberately stays dependency-free: adjacency is kept in plain
    dictionaries/sets rather than pulling in a graph library. A bounded node
    cap (``max_nodes``) bounds the memory footprint; when the cap is reached
    the oldest placeholder nodes are evicted first and the event is recorded
    in ``evictions`` so callers can observe the bound being applied.
    """

    def __init__(self, max_nodes: int | None = None) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[tuple[str, str, EdgeType, str | None], GraphEdge] = {}
        # node id -> outgoing edge keys, so edge removal never scans the graph
        self.out_edges: dict[str, set[tuple[str, str, EdgeType, str | None]]] = {}
        # file path -> node ids defined in that file (the reverse index)
        self.file_index: dict[str, set[str]] = {}
        # node id -> files that contain the node (usually one, placeholders have none)
        self.node_files: dict[str, set[str]] = {}
        # node id -> incoming edge node ids, used to propagate incremental updates
        self.reverse_adjacency: dict[str, set[str]] = {}
        self.fingerprints: dict[str, FileFingerprint] = {}
        self.max_nodes = max_nodes
        self.evictions: list[str] = []

    def add_node(self, node: GraphNode, index_file: bool = True) -> None:
        if node.node_id not in self.nodes and self.max_nodes is not None and len(self.nodes) >= self.max_nodes:
            evicted = self._evict_one()
            if evicted is None:
                node.warnings.append(f"node cap {self.max_nodes} reached; node retained without eviction")
        self.nodes[node.node_id] = node
        self.node_files.setdefault(node.node_id, set())
        if index_file and node.source_file:
            self.file_index.setdefault(node.source_file, set()).add(node.node_id)
            self.node_files[node.node_id].add(node.source_file)

    def _evict_one(self) -> str | None:
        """Drop one placeholder node first, otherwise the oldest contained node."""

        victim: str | None = None
        for node_id, node in self.nodes.items():
            if node.status == NodeStatus.PLACEHOLDER:
                victim = node_id
                break
        if victim is None:
            victim = next(iter(self.nodes), None)
        if victim is None:
            return None
        self.remove_node(victim)
        self.evictions.append(victim)
        return victim

    def remove_node(self, node_id: str) -> None:
        node = self.nodes.pop(node_id, None)
        if node is None:
            return
        for path in self.node_files.pop(node_id, set()):
            ids = self.file_index.get(path)
            if ids is not None:
                ids.discard(node_id)
                if not ids:
                    self.file_index.pop(path, None)
        incident = set(self.out_edges.get(node_id, set()))
        for referrer in list(self.reverse_adjacency.get(node_id, set())):
            incident.update(self.out_edges.get(referrer, set()))
        for edge_key in [key for key in incident if key[0] == node_id or key[1] == node_id]:
            edge = self.edges.pop(edge_key)
            self.out_edges.get(edge.source, set()).discard(edge_key)
            incoming = self.reverse_adjacency.get(edge.target)
            if incoming is not None:
                incoming.discard(edge.source)
        self.reverse_adjacency.pop(node_id, None)
        self.out_edges.pop(node_id, None)

    def add_edge(self, edge: GraphEdge) -> None:
        key = edge.key()
        self.edges[key] = edge
        self.out_edges.setdefault(edge.source, set()).add(key)
        self.reverse_adjacency.setdefault(edge.target, set()).add(edge.source)

    def remove_edges_of_node(self, node_id: str) -> None:
        for edge_key in list(self.out_edges.get(node_id, set())):
            edge = self.edges.pop(edge_key)
            incoming = self.reverse_adjacency.get(edge.target)
            if incoming is not None:
                incoming.discard(edge.source)
            self.out_edges.get(node_id, set()).discard(edge_key)

    def nodes_for_file(self, path: str) -> set[str]:
        return set(self.file_index.get(path, set()))

    def referrers_of_file(self, path: str) -> set[str]:
        """All nodes that hold an edge pointing at a node of ``path``."""

        result: set[str] = set()
        for node_id in self.nodes_for_file(path):
            result.update(self.referrers(node_id))
        return result

    def referrers(self, node_id: str) -> set[str]:
        """Node ids whose edges point at ``node_id`` (one-hop reverse adjacency)."""

        return set(self.reverse_adjacency.get(node_id, set()))

    def outgoing(self, node_id: str) -> list[GraphEdge]:
        return [self.edges[key] for key in self.out_edges.get(node_id, set()) if key in self.edges]

    def file_count(self) -> int:
        return len(self.file_index)

    def approx_memory_bytes(self) -> int:
        """Rough, allocation-free memory estimate used for bounded-cache reporting."""

        node_bytes = sum(
            len(node.node_id) + len(node.canonical_type) + len(node.source_file)
            + len(str(node.attributes))
            + sum(len(k) + len(v) for k, v in node.framework_types.items())
            for node in self.nodes.values()
        )
        edge_bytes = sum(
            len(edge.source) + len(edge.target) + len(edge.edge_type.value) + len(edge.attribute or "")
            for edge in self.edges.values()
        )
        return (node_bytes + edge_bytes) * 2

    def warnings(self) -> list[str]:
        return [f"{node.node_id}: {warning}" for node in self.nodes.values() for warning in node.warnings]

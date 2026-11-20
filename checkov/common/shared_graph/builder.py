from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field

from checkov.common.shared_graph.adapters import (
    DEFAULT_ADAPTERS,
    ExtractedReference,
    ExtractedResource,
    FrameworkAdapter,
)
from checkov.common.shared_graph.config import SharedGraphConfig
from checkov.common.shared_graph.file_parser import ParsedDocument, fingerprint_file, parse_file
from checkov.common.shared_graph.mapping import LabelMapping
from checkov.common.shared_graph.models import EdgeType, GraphEdge, GraphNode, NodeStatus, UnifiedResourceGraph

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    graph: UnifiedResourceGraph
    parse_failures: list[str] = field(default_factory=list)
    placeholder_nodes: list[str] = field(default_factory=list)
    rebuilt: bool = False


class SharedGraphBuilder:
    """Builds and incrementally maintains the :class:`UnifiedResourceGraph`.

    Each input file is parsed exactly once; every compatible framework adapter
    then reads the in-memory parse result instead of re-parsing. Cross-file
    references are resolved here, at graph-build time, so no framework has to
    implement its own reference resolution.
    """

    def __init__(
        self,
        config: SharedGraphConfig,
        mapping: LabelMapping,
        adapters: tuple[FrameworkAdapter, ...] = DEFAULT_ADAPTERS,
    ) -> None:
        self.config = config
        self.mapping = mapping
        self.adapters = adapters
        self._adapter_by_name = {adapter.name: adapter for adapter in adapters}

    def build(self, files: list[str]) -> BuildResult:
        graph = UnifiedResourceGraph(max_nodes=self.config.max_nodes)
        parse_failures: list[str] = []
        parsed: list[tuple[ParsedDocument, list[FrameworkAdapter]]] = []
        for path in files:
            document = parse_file(path)
            graph.fingerprints[path] = document.fingerprint
            if not document.ok:
                parse_failures.append(path)
                graph.add_node(self._placeholder_for_file(path, document.parse_error or "parse error"))
                continue
            adapters = self._adapters_for(path, document)
            if adapters:
                parsed.append((document, adapters))

        # Phase 1: every resource becomes a node before any edge is resolved so
        # references can point at resources defined in later files.
        for document, adapters in parsed:
            for adapter in adapters:
                for raw in self._resources_of(document, adapter):
                    graph.add_node(self._to_node(adapter, document.path, raw))

        # Phase 2: cross-file references become edges (or placeholders).
        placeholders: list[str] = []
        for document, adapters in parsed:
            for adapter in adapters:
                resources = self._resources_of(document, adapter)
                for reference in adapter.extract_references(document.path, resources, self.mapping):
                    self._link_reference(graph, adapter, document.path, reference, placeholders)

        return BuildResult(graph=graph, parse_failures=parse_failures, placeholder_nodes=placeholders)

    def update(
        self,
        graph: UnifiedResourceGraph,
        changed_files: list[str] | None = None,
        deleted_files: list[str] | None = None,
    ) -> BuildResult:
        """Refresh only the subgraph touched by the given files.

        The reverse file index yields the nodes defined in each changed file;
        reverse adjacency adds the nodes referencing them. Only that set (and
        its edges) is recomputed. If the fan-out exceeds the configured cap a
        full rebuild is returned instead.
        """

        changed_files = changed_files or []
        deleted_files = deleted_files or []
        affected = self._affected_nodes(graph, changed_files, deleted_files)
        if len(affected) > self.config.affected_node_cap:
            logger.info(
                "shared graph: affected set %d exceeds cap %d, falling back to full rebuild",
                len(affected),
                self.config.affected_node_cap,
            )
            all_files = sorted((set(graph.file_index) | set(changed_files)) - set(deleted_files))
            result = self.build(all_files)
            result.rebuilt = True
            return result

        referrer_nodes = {node_id for path in changed_files for node_id in graph.referrers_of_file(path)}
        self._detach_affected(graph, affected)

        parse_failures: list[str] = []
        placeholders: list[str] = []
        for path in changed_files:
            current = fingerprint_file(path)
            cached = graph.fingerprints.get(path)
            if cached is not None and cached.cheap_matches(current):
                # The file is part of the batch but its content never changed.
                graph.fingerprints[path] = current
                continue
            document = parse_file(path)
            graph.fingerprints[path] = document.fingerprint
            if not document.ok:
                parse_failures.append(path)
                graph.add_node(self._placeholder_for_file(path, document.parse_error or "parse error"))
                continue
            for adapter in self._adapters_for(path, document):
                resources = self._resources_of(document, adapter)
                for raw in resources:
                    graph.add_node(self._to_node(adapter, path, raw))
                for reference in adapter.extract_references(path, resources, self.mapping):
                    self._link_reference(graph, adapter, path, reference, placeholders)

        for path in deleted_files:
            graph.fingerprints.pop(path, None)

        # Re-link referrers whose edges pointed at the replaced subgraph.
        for node_id in referrer_nodes:
            if node_id in affected and node_id not in graph.nodes:
                continue
            self._relink_node(graph, node_id, placeholders)

        # References that used to resolve into a deleted file now become
        # placeholders rather than dangling edges.
        self._replace_dangling_edges(graph, placeholders)
        return BuildResult(graph=graph, parse_failures=parse_failures, placeholder_nodes=placeholders)

    def _adapters_for(self, path: str, document: ParsedDocument | None = None) -> list[FrameworkAdapter]:
        candidates = [adapter for adapter in self.adapters if adapter.supports_file(path)]
        if document is None or len(candidates) <= 1:
            return candidates
        # The parse result is shared, but each document is claimed by the first
        # adapter that recognises its shape: a YAML file is therefore walked
        # once instead of once per framework, while every framework still sees
        # its own resources through its own adapter.
        for adapter in candidates:
            if any(adapter.extract_resources(path, raw) for raw in document.documents):
                return [adapter]
        return candidates[:1]

    @staticmethod
    def _resources_of(document: ParsedDocument, adapter: FrameworkAdapter) -> list[ExtractedResource]:
        resources: list[ExtractedResource] = []
        for raw_document in document.documents:
            resources.extend(adapter.extract_resources(document.path, raw_document))
        return resources

    def _to_node(self, adapter: FrameworkAdapter, path: str, resource: ExtractedResource) -> GraphNode:
        canonical = self.mapping.to_canonical(adapter.name, resource.native_type)
        node_id = self.node_id(adapter.name, path, resource.resource_id)
        return GraphNode(
            node_id=node_id,
            canonical_type=canonical,
            source_file=path,
            attributes=resource.attributes,
            framework_types={adapter.name: resource.native_type},
            line_number=resource.line_number,
        )

    @staticmethod
    def node_id(framework: str, path: str, resource_id: str) -> str:
        return f"{framework}:{path}#{resource_id}"

    def _placeholder_for_file(self, path: str, reason: str) -> GraphNode:
        node = GraphNode(
            node_id=f"placeholder:{path}",
            canonical_type="unresolved.file",
            source_file=path,
            status=NodeStatus.PLACEHOLDER,
            warnings=[f"file could not be parsed: {reason}"],
        )
        return node

    def _link_reference(
        self,
        graph: UnifiedResourceGraph,
        adapter: FrameworkAdapter,
        source_path: str,
        reference: ExtractedReference,
        placeholders: list[str],
    ) -> None:
        source_id = self.node_id(adapter.name, source_path, reference.source_resource_id)
        if source_id not in graph.nodes:
            return
        target_path = self._resolve_target_path(source_path, reference.target_file)
        targets = self._find_targets(graph, target_path, reference.target_resource_id)
        if not targets:
            placeholder_id = f"placeholder:{target_path}#{reference.target_resource_id or '*'}"
            if placeholder_id not in graph.nodes:
                graph.add_node(
                    GraphNode(
                        node_id=placeholder_id,
                        canonical_type="unresolved.reference",
                        source_file=target_path,
                        status=NodeStatus.PLACEHOLDER,
                        warnings=[
                            f"referenced resource {reference.target_resource_id or '*'} could not be resolved"
                        ],
                    ),
                    index_file=False,
                )
                placeholders.append(placeholder_id)
            targets = [placeholder_id]
        for target_id in targets:
            graph.add_edge(
                GraphEdge(
                    source=source_id,
                    target=target_id,
                    edge_type=EdgeType.REFERENCES,
                    attribute=reference.attribute,
                )
            )

    @staticmethod
    def _resolve_target_path(source_path: str, target_file: str) -> str:
        if os.path.isabs(target_file):
            return os.path.normpath(target_file)
        return os.path.normpath(os.path.join(os.path.dirname(source_path), target_file))

    @staticmethod
    def _find_targets(graph: UnifiedResourceGraph, target_path: str, resource_id: str | None) -> list[str]:
        candidates = graph.nodes_for_file(target_path)
        if not candidates:
            # Tolerate callers mixing relative and normalized paths.
            candidates = {
                node_id
                for node_id, node in graph.nodes.items()
                if os.path.basename(node.source_file) == os.path.basename(target_path)
            }
        if resource_id is None:
            return sorted(node_id for node_id in candidates if not node_id.startswith("placeholder:"))
        return sorted(node_id for node_id in candidates if node_id.endswith(f"#{resource_id}"))

    def _affected_nodes(
        self, graph: UnifiedResourceGraph, changed_files: list[str], deleted_files: list[str]
    ) -> set[str]:
        affected: set[str] = set()
        for path in [*changed_files, *deleted_files]:
            affected.update(graph.nodes_for_file(path))
        # One-hop reverse adjacency: nodes referencing a replaced node must be
        # re-linked, but their parsed content is untouched.
        for node_id in list(affected):
            affected.update(graph.referrers(node_id))
        return affected

    def _detach_affected(self, graph: UnifiedResourceGraph, affected: set[str]) -> None:
        for node_id in affected:
            graph.remove_edges_of_node(node_id)
        for node_id in affected:
            incoming = graph.reverse_adjacency.get(node_id, set())
            for referrer in list(incoming):
                graph.remove_edges_of_node(referrer)
        # Remove only nodes defined by changed files; pure referrers stay.
        for node_id in list(affected):
            node = graph.nodes.get(node_id)
            if node and node.source_file and node_id in graph.nodes_for_file(node.source_file):
                graph.remove_node(node_id)

    def _relink_node(self, graph: UnifiedResourceGraph, node_id: str, placeholders: list[str]) -> None:
        node = graph.nodes.get(node_id)
        if node is None or node.status == NodeStatus.PLACEHOLDER:
            return
        framework = next(iter(node.framework_types), "")
        adapter = self._adapter_by_name.get(framework)
        if adapter is None:
            return
        synthetic = ExtractedResource(
            resource_id=node_id.rsplit("#", 1)[-1],
            native_type=node.native_type(framework),
            attributes=node.attributes,
        )
        for reference in adapter.extract_references(node.source_file, [synthetic], self.mapping):
            self._link_reference(graph, adapter, node.source_file, reference, placeholders)

    def _replace_dangling_edges(self, graph: UnifiedResourceGraph, placeholders: list[str]) -> None:
        dangling: list[GraphEdge] = []
        for edge in graph.edges.values():
            if edge.target not in graph.nodes:
                dangling.append(edge)
        for edge in dangling:
            placeholder_id = f"placeholder:{edge.target}"
            if placeholder_id not in graph.nodes:
                graph.add_node(
                    GraphNode(
                        node_id=placeholder_id,
                        canonical_type="unresolved.reference",
                        source_file="",
                        status=NodeStatus.PLACEHOLDER,
                        warnings=["target node vanished after incremental update"],
                    ),
                    index_file=False,
                )
                placeholders.append(placeholder_id)
            graph.remove_edges_of_node(edge.source)
            graph.add_edge(
                GraphEdge(
                    source=edge.source,
                    target=placeholder_id,
                    edge_type=edge.edge_type,
                    attribute=edge.attribute,
                )
            )

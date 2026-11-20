from __future__ import annotations

from dataclasses import dataclass, field

from checkov.common.shared_graph.adapters import FrameworkAdapter
from checkov.common.shared_graph.mapping import LabelMapping
from checkov.common.shared_graph.models import NodeStatus, UnifiedResourceGraph


@dataclass
class FrameworkFinding:
    framework: str
    node_id: str
    source_file: str
    resource_id: str
    native_type: str
    check_id: str
    line_number: int | None = None


@dataclass
class FrameworkViewResult:
    framework: str
    findings: list[FrameworkFinding] = field(default_factory=list)
    placeholder_nodes: list[str] = field(default_factory=list)


class SharedGraphRunner:
    """Runs one framework's checks on the shared graph.

    The check predicate is the adapter's own :meth:`check` method — the same
    code the per-framework fallback executes — so adopting the shared graph
    cannot change any judgement. Nodes are presented under the framework's
    native label; the canonical label never leaks into reports.
    """

    def __init__(self, graph: UnifiedResourceGraph, mapping: LabelMapping) -> None:
        self.graph = graph
        self.mapping = mapping

    def run_framework(self, adapter: FrameworkAdapter) -> FrameworkViewResult:
        result = FrameworkViewResult(framework=adapter.name)
        framework_placeholders = {
            edge.target
            for edge in self.graph.edges.values()
            if edge.source in self.graph.nodes
            and adapter.name in self.graph.nodes[edge.source].framework_types
            and edge.target in self.graph.nodes
            and self.graph.nodes[edge.target].status == NodeStatus.PLACEHOLDER
        }
        # File-level placeholders (the whole file failed to parse) are reported
        # by every framework that would have scanned that file.
        for node in self.graph.nodes.values():
            if node.status == NodeStatus.PLACEHOLDER and node.canonical_type == "unresolved.file":
                if adapter.supports_file(node.source_file):
                    framework_placeholders.add(node.node_id)
        for node in self.graph.nodes.values():
            if adapter.name not in node.framework_types and node.status == NodeStatus.RESOLVED:
                continue
            if node.status == NodeStatus.PLACEHOLDER:
                if node.node_id in framework_placeholders:
                    result.placeholder_nodes.append(node.node_id)
                continue
            native_type = node.native_type(adapter.name)
            for check_id in adapter.check(native_type, node.attributes):
                result.findings.append(
                    FrameworkFinding(
                        framework=adapter.name,
                        node_id=node.node_id,
                        source_file=node.source_file,
                        resource_id=node.node_id.rsplit("#", 1)[-1],
                        native_type=native_type,
                        check_id=check_id,
                        line_number=node.line_number,
                    )
                )
        return result

from __future__ import annotations

from dataclasses import dataclass

from checkov.common.shared_graph.adapters import FrameworkAdapter
from checkov.common.shared_graph.file_parser import parse_file
from checkov.common.shared_graph.runner import FrameworkFinding


@dataclass
class BaselineResult:
    """Findings produced by the legacy per-framework parse path."""

    findings: list[FrameworkFinding]
    parse_failures: list[str]


class PerFrameworkBaseline:
    """Reference implementation: every framework parses every file itself.

    This is both the degradation path used when graph construction fails and
    the oracle used by the acceptance benchmark to prove the shared graph
    reports the same findings.
    """

    def __init__(self) -> None:
        self.parse_calls = 0

    def run(self, files: list[str], adapters: list[FrameworkAdapter]) -> BaselineResult:
        findings: list[FrameworkFinding] = []
        parse_failures: list[str] = []
        for path in files:
            for adapter in adapters:
                if not adapter.supports_file(path):
                    continue
                # Each framework owns its parser in the legacy path, so the
                # same file is re-parsed for every compatible framework.
                document = parse_file(path)
                self.parse_calls += 1
                if not document.ok:
                    if path not in parse_failures:
                        parse_failures.append(path)
                    continue
                for raw_document in document.documents:
                    for resource in adapter.extract_resources(path, raw_document):
                        for check_id in adapter.check(resource.native_type, resource.attributes):
                            findings.append(
                                FrameworkFinding(
                                    framework=adapter.name,
                                    node_id=f"{adapter.name}:{path}#{resource.resource_id}",
                                    source_file=path,
                                    resource_id=resource.resource_id,
                                    native_type=resource.native_type,
                                    check_id=check_id,
                                    line_number=resource.line_number,
                                )
                            )
        return BaselineResult(findings=findings, parse_failures=parse_failures)

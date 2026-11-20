from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from checkov.common.shared_graph.adapters import DEFAULT_ADAPTERS, FrameworkAdapter
from checkov.common.shared_graph.baseline import BaselineResult, PerFrameworkBaseline
from checkov.common.shared_graph.builder import BuildResult, SharedGraphBuilder
from checkov.common.shared_graph.cache import GraphCache
from checkov.common.shared_graph.config import SharedGraphConfig, UpdateTrigger
from checkov.common.shared_graph.mapping import LabelMapping
from checkov.common.shared_graph.models import UnifiedResourceGraph
from checkov.common.shared_graph.runner import FrameworkViewResult, SharedGraphRunner

logger = logging.getLogger(__name__)


@dataclass
class ScanTimings:
    """First-build and incremental costs are reported separately on purpose."""

    parse_seconds: float = 0.0
    graph_build_seconds: float = 0.0
    incremental_seconds: float = 0.0
    check_seconds: float = 0.0
    total_seconds: float = 0.0


@dataclass
class ScanOutcome:
    findings_by_framework: dict[str, FrameworkViewResult]
    timings: ScanTimings
    degraded: bool = False
    used_cache: bool = False
    incremental: bool = False
    rebuilt: bool = False
    warnings: list[str] = field(default_factory=list)
    graph: UnifiedResourceGraph | None = None
    baseline: BaselineResult | None = None


class SharedGraphService:
    """Entry point combining build, cache, incremental update and degradation.

    The service is disabled by default and every public method degrades to the
    per-framework baseline on failure, so a graph problem can never make a
    scan produce zero results.
    """

    def __init__(
        self,
        config: SharedGraphConfig | None = None,
        adapters: tuple[FrameworkAdapter, ...] = DEFAULT_ADAPTERS,
        cache: GraphCache | None = None,
    ) -> None:
        self.config = config or SharedGraphConfig()
        self.mapping = self._load_mapping()
        self.builder = SharedGraphBuilder(self.config, self.mapping, adapters)
        self.adapters = adapters
        self.baseline = PerFrameworkBaseline()
        self.cache = cache
        self.graph: UnifiedResourceGraph | None = None
        self._pending_changes: list[str] = []
        self._pending_deletions: list[str] = []

    def _load_mapping(self) -> LabelMapping:
        try:
            return LabelMapping.from_file(self.config.mapping_file)
        except (OSError, ValueError) as error:
            logger.warning("shared graph: mapping %s unusable (%s); using empty mapping",
                           self.config.mapping_file, error)
            return LabelMapping()

    def scan(self, files: list[str]) -> ScanOutcome:
        """Full scan. When disabled, transparently runs the legacy baseline."""

        started = time.perf_counter()
        if not self.config.enabled:
            return self._degraded_scan(files, started, reason="shared graph disabled (default)")
        try:
            timings = ScanTimings()
            graph = self._load_or_build(files, timings)
            self.graph = graph
            outcomes = self._run_checks(graph, timings)
            timings.total_seconds = time.perf_counter() - started
            return ScanOutcome(
                findings_by_framework=outcomes,
                timings=timings,
                used_cache=self.cache is not None and self._last_used_cache,
                warnings=graph.warnings(),
                graph=graph,
            )
        except Exception as error:  # noqa: BLE001 - degradation must catch everything
            logger.exception("shared graph build failed: %s", error)
            return self._degraded_scan(files, started, reason=f"graph build failed: {error}")

    def notify_changed(self, path: str) -> ScanOutcome | None:
        """Record a file save.

        IMMEDIATE applies the incremental update right away; BATCHED buffers
        the change until :meth:`flush` or the configured threshold is reached.
        """

        if not self.config.enabled or self.graph is None:
            return None
        if self.config.update_trigger == UpdateTrigger.IMMEDIATE:
            return self.apply_changes(changed_files=[path])
        self._pending_changes.append(path)
        if len(self._pending_changes) >= self.config.batched_threshold:
            return self.flush()
        return None

    def notify_deleted(self, path: str) -> None:
        self._pending_deletions.append(path)
        if self.config.update_trigger == UpdateTrigger.IMMEDIATE:
            self.flush()

    def flush(self) -> ScanOutcome | None:
        if self.graph is None or not self._pending_changes and not self._pending_deletions:
            self._pending_changes.clear()
            self._pending_deletions.clear()
            return None
        changed = self._drain(self._pending_changes)
        deleted = self._drain(self._pending_deletions)
        return self.apply_changes(changed, deleted)

    def apply_changes(
        self, changed_files: list[str] | None = None, deleted_files: list[str] | None = None
    ) -> ScanOutcome:
        """Incremental update; degrades to baseline when the update fails."""

        started = time.perf_counter()
        if self.graph is None:
            raise RuntimeError("call scan() before apply_changes()")
        try:
            timings = ScanTimings()
            update_started = time.perf_counter()
            result: BuildResult = self.builder.update(
                self.graph, changed_files=changed_files or [], deleted_files=deleted_files or []
            )
            timings.incremental_seconds = time.perf_counter() - update_started
            self.graph = result.graph
            if self.cache is not None:
                self.cache.store(self.graph)
            outcomes = self._run_checks(self.graph, timings)
            timings.total_seconds = time.perf_counter() - started
            return ScanOutcome(
                findings_by_framework=outcomes,
                timings=timings,
                incremental=not result.rebuilt,
                rebuilt=result.rebuilt,
                warnings=self.graph.warnings(),
                graph=self.graph,
            )
        except Exception as error:  # noqa: BLE001
            logger.exception("shared graph incremental update failed: %s", error)
            all_files = sorted((set(self.graph.file_index) | set(changed_files or [])) - set(deleted_files or []))
            return self._degraded_scan(all_files, started, reason=f"incremental update failed: {error}")

    def _load_or_build(self, files: list[str], timings: ScanTimings) -> UnifiedResourceGraph:
        self._last_used_cache = False
        if self.cache is not None:
            cached = self.cache.load(files)
            if cached is not None:
                self._last_used_cache = True
                return cached
        build_started = time.perf_counter()
        result = self.builder.build(files)
        timings.graph_build_seconds = time.perf_counter() - build_started
        timings.parse_seconds = timings.graph_build_seconds
        if self.cache is not None:
            self.cache.store(result.graph)
        return result.graph

    def _run_checks(
        self, graph: UnifiedResourceGraph, timings: ScanTimings
    ) -> dict[str, FrameworkViewResult]:
        runner = SharedGraphRunner(graph, self.mapping)
        check_started = time.perf_counter()
        outcomes = {adapter.name: runner.run_framework(adapter) for adapter in self.adapters}
        timings.check_seconds = time.perf_counter() - check_started
        return outcomes

    def _degraded_scan(self, files: list[str], started: float, reason: str) -> ScanOutcome:
        baseline_result = self.baseline.run(files, list(self.adapters))
        outcomes: dict[str, FrameworkViewResult] = {}
        for adapter in self.adapters:
            findings = [finding for finding in baseline_result.findings if finding.framework == adapter.name]
            outcomes[adapter.name] = FrameworkViewResult(framework=adapter.name, findings=findings)
        timings = ScanTimings(total_seconds=time.perf_counter() - started)
        return ScanOutcome(
            findings_by_framework=outcomes,
            timings=timings,
            degraded=True,
            warnings=[reason, *[f"parse failure: {path}" for path in baseline_result.parse_failures]],
            baseline=baseline_result,
        )

    @staticmethod
    def _drain(pending: list[str]) -> list[str]:
        unique = sorted(set(pending))
        pending.clear()
        return unique

    def save_cache(self, path: str | Path) -> None:
        if self.graph is None:
            return
        GraphCache(path, max_cached_files=self.config.max_cached_files).store(self.graph)

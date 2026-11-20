from __future__ import annotations

from checkov.common.shared_graph.adapters import DEFAULT_ADAPTERS
from checkov.common.shared_graph.baseline import PerFrameworkBaseline
from checkov.common.shared_graph.cache import GraphCache
from checkov.common.shared_graph.config import SharedGraphConfig, UpdateTrigger
from checkov.common.shared_graph.models import EdgeType
from checkov.common.shared_graph.runner import SharedGraphRunner
from checkov.common.shared_graph.service import SharedGraphService


def _files(sample_repo) -> list[str]:
    return [
        str(sample_repo / "bucket.yaml"),
        str(sample_repo / "generic.yaml"),
        str(sample_repo / "shared" / "policy.yaml"),
    ]


def test_disabled_service_uses_per_framework_baseline(sample_repo) -> None:
    service = SharedGraphService(SharedGraphConfig(enabled=False))

    outcome = service.scan(_files(sample_repo))

    assert outcome.degraded
    assert outcome.graph is None
    assert outcome.baseline is not None


def test_shared_graph_findings_match_per_framework_baseline(sample_repo) -> None:
    files = _files(sample_repo)
    service = SharedGraphService(SharedGraphConfig(enabled=True))

    outcome = service.scan(files)
    baseline = PerFrameworkBaseline().run(files, list(DEFAULT_ADAPTERS))

    shared_findings = {
        (finding.framework, finding.source_file, finding.resource_id, finding.native_type, finding.check_id)
        for framework_result in outcome.findings_by_framework.values()
        for finding in framework_result.findings
    }
    baseline_findings = {
        (finding.framework, finding.source_file, finding.resource_id, finding.native_type, finding.check_id)
        for finding in baseline.findings
    }
    assert shared_findings == baseline_findings


def test_native_labels_are_restored_in_reports(sample_repo) -> None:
    outcome = SharedGraphService(SharedGraphConfig(enabled=True)).scan(_files(sample_repo))

    cfn_findings = outcome.findings_by_framework["cloudformation"].findings
    assert cfn_findings
    assert all(finding.native_type.startswith("AWS::") for finding in cfn_findings)
    generic_findings = outcome.findings_by_framework["generic_yaml"].findings
    assert all(not finding.native_type.startswith("storage.") for finding in generic_findings)


def test_build_failure_degrades_to_baseline(sample_repo, monkeypatch) -> None:
    service = SharedGraphService(SharedGraphConfig(enabled=True))

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated builder failure")

    monkeypatch.setattr(service.builder, "build", boom)

    outcome = service.scan(_files(sample_repo))

    assert outcome.degraded
    assert outcome.baseline is not None
    assert any("graph build failed" in warning for warning in outcome.warnings)


def test_batched_trigger_buffers_until_threshold(sample_repo) -> None:
    config = SharedGraphConfig(enabled=True, update_trigger=UpdateTrigger.BATCHED, batched_threshold=2)
    service = SharedGraphService(config)
    service.scan(_files(sample_repo))

    assert service.notify_changed(str(sample_repo / "bucket.yaml")) is None
    flushed = service.notify_changed(str(sample_repo / "generic.yaml"))

    assert flushed is not None
    assert flushed.incremental


def test_immediate_trigger_updates_on_each_save(sample_repo) -> None:
    service = SharedGraphService(SharedGraphConfig(enabled=True))
    service.scan(_files(sample_repo))

    outcome = service.notify_changed(str(sample_repo / "bucket.yaml"))

    assert outcome is not None
    assert outcome.incremental


def test_cache_is_reused_when_files_unchanged(sample_repo, tmp_path) -> None:
    cache = GraphCache(tmp_path / "graph.json")
    service = SharedGraphService(SharedGraphConfig(enabled=True), cache=cache)
    files = _files(sample_repo)
    first = service.scan(files)
    assert not first.used_cache

    second_service = SharedGraphService(SharedGraphConfig(enabled=True), cache=cache)
    second = second_service.scan(files)

    assert second.used_cache
    assert second.graph is not None
    assert second.graph.nodes.keys() == first.graph.nodes.keys()


def test_runner_reports_placeholder_only_for_referring_framework(sample_repo) -> None:
    service = SharedGraphService(SharedGraphConfig(enabled=True))
    outcome = service.scan(_files(sample_repo))

    runner = SharedGraphRunner(outcome.graph, service.mapping)
    cfn = runner.run_framework(DEFAULT_ADAPTERS[0])
    k8s = runner.run_framework(DEFAULT_ADAPTERS[1])

    assert cfn.placeholder_nodes == []
    assert k8s.placeholder_nodes == []


def test_cross_file_edge_survives_incremental_update(sample_repo) -> None:
    service = SharedGraphService(SharedGraphConfig(enabled=True))
    service.scan(_files(sample_repo))
    bucket = sample_repo / "bucket.yaml"
    bucket.write_text(bucket.read_text() + "# touched\n")

    outcome = service.apply_changes(changed_files=[str(bucket)])

    edges = [edge for edge in outcome.graph.edges.values() if edge.edge_type == EdgeType.REFERENCES]
    assert any(edge.target.endswith("shared/policy.yaml#BasePolicy") for edge in edges)
    assert all(edge.target in outcome.graph.nodes for edge in edges)

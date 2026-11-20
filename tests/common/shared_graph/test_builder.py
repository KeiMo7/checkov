from __future__ import annotations

from checkov.common.shared_graph.adapters import DEFAULT_ADAPTERS
from checkov.common.shared_graph.builder import SharedGraphBuilder
from checkov.common.shared_graph.config import SharedGraphConfig
from checkov.common.shared_graph.mapping import LabelMapping
from checkov.common.shared_graph.models import EdgeType, NodeStatus


def _builder(config: SharedGraphConfig | None = None) -> SharedGraphBuilder:
    mapping = LabelMapping.from_file(SharedGraphConfig.DEFAULT_MAPPING_FILE)
    return SharedGraphBuilder(config or SharedGraphConfig(enabled=True), mapping, DEFAULT_ADAPTERS)


def test_each_file_parsed_once_and_shared_across_frameworks(sample_repo) -> None:
    files = [
        str(sample_repo / "bucket.yaml"),
        str(sample_repo / "generic.yaml"),
        str(sample_repo / "shared" / "policy.yaml"),
    ]
    result = _builder().build(files)
    graph = result.graph

    assert graph.file_count() == 3
    # One fingerprint per file regardless of how many frameworks consume it.
    assert sorted(graph.fingerprints) == sorted(files)
    assert any(node.canonical_type == "storage.bucket" for node in graph.nodes.values())
    assert any(node.canonical_type == "iam.policy" for node in graph.nodes.values())


def test_cross_file_references_resolve_at_build_time(sample_repo) -> None:
    files = [
        str(sample_repo / "bucket.yaml"),
        str(sample_repo / "generic.yaml"),
        str(sample_repo / "shared" / "policy.yaml"),
    ]
    graph = _builder().build(files).graph

    reference_edges = [edge for edge in graph.edges.values() if edge.edge_type == EdgeType.REFERENCES]
    targets = {edge.target for edge in reference_edges}
    assert any(target.endswith("shared/policy.yaml#BasePolicy") for target in targets)
    assert all(target in graph.nodes for target in targets)


def test_unresolved_reference_keeps_annotated_placeholder(sample_repo) -> None:
    (sample_repo / "dangling.yaml").write_text(
        "resources:\n"
        "  - name: orphan\n"
        "    type: bucket\n"
        "    link: '$ref: missing/file.yaml#Thing'\n"
    )
    result = _builder().build([str(sample_repo / "dangling.yaml")])

    placeholders = [node for node in result.graph.nodes.values() if node.status == NodeStatus.PLACEHOLDER]
    assert placeholders
    assert all(node.warnings for node in placeholders)


def test_broken_file_becomes_file_level_placeholder(sample_repo) -> None:
    broken = sample_repo / "broken.yaml"
    broken.write_text("Resources:\n  Bad: {unterminated: [\n")

    result = _builder().build([str(broken)])

    assert str(broken) in result.parse_failures
    node = result.graph.nodes[f"placeholder:{broken}"]
    assert node.status == NodeStatus.PLACEHOLDER
    assert node.canonical_type == "unresolved.file"


def test_reverse_index_locates_nodes_for_changed_file(sample_repo) -> None:
    bucket = str(sample_repo / "bucket.yaml")
    graph = _builder().build([bucket, str(sample_repo / "shared" / "policy.yaml")]).graph

    node_ids = graph.nodes_for_file(bucket)
    assert node_ids and all(graph.nodes[node_id].source_file == bucket for node_id in node_ids)


def test_incremental_update_only_recomputes_affected_subgraph(sample_repo) -> None:
    bucket = sample_repo / "bucket.yaml"
    policy = sample_repo / "shared" / "policy.yaml"
    files = [str(bucket), str(policy)]
    builder = _builder()
    graph = builder.build(files).graph
    original_policy_node = next(iter(graph.nodes_for_file(str(policy))))

    bucket.write_text(
        "Resources:\n"
        "  DataBucket:\n"
        "    Type: AWS::S3::Bucket\n"
        "    Properties:\n"
        "      BucketEncryption:\n"
        "        SSEConfiguration: []\n"
        "      Policy: '!ImportValue shared/policy.yaml#BasePolicy'\n"
    )
    result = builder.update(graph, changed_files=[str(bucket)])

    assert not result.rebuilt
    # Untouched file keeps its node identity.
    assert original_policy_node in result.graph.nodes
    bucket_nodes = result.graph.nodes_for_file(str(bucket))
    assert len(bucket_nodes) == 1
    assert any(edge.target.endswith("#BasePolicy") for edge in result.graph.edges.values())


def test_incremental_update_handles_deletion(sample_repo) -> None:
    bucket = str(sample_repo / "bucket.yaml")
    policy = str(sample_repo / "shared" / "policy.yaml")
    builder = _builder()
    graph = builder.build([bucket, policy]).graph

    result = builder.update(graph, deleted_files=[policy])

    assert not result.graph.nodes_for_file(policy)
    # The dangling reference is replaced by an annotated placeholder, not a broken edge.
    assert all(edge.target in result.graph.nodes for edge in result.graph.edges.values())


def test_affected_cap_forces_full_rebuild(sample_repo) -> None:
    config = SharedGraphConfig(enabled=True, affected_node_cap=0)
    builder = _builder(config)
    bucket = str(sample_repo / "bucket.yaml")
    graph = builder.build([bucket]).graph

    result = builder.update(graph, changed_files=[bucket])

    assert result.rebuilt


def test_node_cap_bounds_memory(sample_repo) -> None:
    config = SharedGraphConfig(enabled=True, max_nodes=1)
    graph = _builder(config).build(
        [str(sample_repo / "bucket.yaml"), str(sample_repo / "shared" / "policy.yaml")]
    ).graph

    assert len(graph.nodes) <= 1
    assert graph.evictions

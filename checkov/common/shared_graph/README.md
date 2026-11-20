# Shared Resource Graph

One parse, one graph, every framework.

Today each framework runner parses every YAML/JSON file itself and rebuilds a
near-identical resource graph with different node labels. This subsystem parses
each file once, builds a single framework-neutral graph, and lets every
framework run its own checks against that graph.

## Guarantees

- **Opt-in, default off.** `SharedGraphConfig(enabled=False)` is the default and
  `CKV_SHARED_GRAPH` is the single environment switch. When disabled the scan is
  byte-for-byte the legacy per-framework path.
- **No new dependencies.** Only the Python standard library and the already
  required `PyYAML` are used.
- **Judgements do not change.** Framework checks live on the framework adapter
  (`FrameworkAdapter.check`) and are reused by both the shared-graph runner and
  the per-framework baseline; the benchmark asserts identical findings.
- **Bounded memory.** `max_nodes` caps graph size (placeholders are evicted
  first, evictions are recorded) and `max_cached_files` caps the disk cache.
- **Fail-closed degradation.** Any build/update failure returns
  `ScanOutcome(degraded=True)` produced by `PerFrameworkBaseline`, so a graph
  problem never zeroes a scan. Unparseable files/resources become placeholder
  nodes carrying an explanatory warning instead of breaking the build.

## Components

- `models.py` — `GraphNode`, `GraphEdge`, `UnifiedResourceGraph` with the file
  reverse index and reverse adjacency used by incremental updates.
- `mapping.py` / `mapping_config.yaml` — configurable bidirectional label map.
  New frameworks register through config or `LabelMapping.register_framework()`
  without touching graph code; canonical labels are restored to the native
  vocabulary in every framework report.
- `file_parser.py` — the single parse path plus cheap `(mtime_ns, size)`
  fingerprints (SHA-256 only paid when cheap metadata changed).
- `builder.py` — full build, build-time cross-file reference resolution, and
  incremental subgraph updates driven by the reverse index.
- `cache.py` — JSON graph cache that is reused only when every fingerprint
  matches; any mismatch rebuilds.
- `adapters.py` — CloudFormation, Kubernetes and generic-YAML adapters and the
  shared check predicates.
- `runner.py` — runs one adapter over the graph and restores native labels.
- `service.py` — facade wiring build, cache, triggers, timings and degradation.

## Incremental updates and triggers

- `UpdateTrigger.IMMEDIATE` applies each file save immediately (low latency).
- `UpdateTrigger.BATCHED` buffers changes until `batched_threshold` or
  `flush()` (efficient, bounded lag).
- Only nodes of the changed files plus their one-hop referrers are recomputed;
  fan-out beyond `affected_node_cap` deliberately triggers a full rebuild.
- Cache consistency uses `mtime_ns + size`, falling back to SHA-256 when the
  cheap check moves. On mismatch the cache is discarded, never trusted.

## Tests and acceptance benchmark

```bash
python -m pytest tests/common/shared_graph -q
python -m tests.common.shared_graph.benchmark_shared_graph
CKV_BENCH_FILES=3000 python -m tests.common.shared_graph.benchmark_shared_graph
```

The benchmark reports first-build vs incremental cost separately, peak memory,
cache reuse, and per-framework finding parity against the per-framework
baseline, and writes `artifacts/shared_graph_benchmark.json`.

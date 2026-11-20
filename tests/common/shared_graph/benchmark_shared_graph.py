"""Acceptance benchmark for the shared resource graph.

Generates a large synthetic repository and compares:

* the legacy per-framework parse path (every framework parses every file),
* the shared-graph path (one parse, one graph, shared checks),
* a single-file incremental update,
* cache reuse.

Outputs a human-readable table plus a JSON artifact under ``artifacts/``.

Run::

    python -m tests.common.shared_graph.benchmark_shared_graph
    CKV_BENCH_FILES=2000 python -m tests.common.shared_graph.benchmark_shared_graph
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import tempfile
import time
import tracemalloc
from pathlib import Path

from checkov.common.shared_graph.adapters import DEFAULT_ADAPTERS
from checkov.common.shared_graph.baseline import PerFrameworkBaseline
from checkov.common.shared_graph.cache import GraphCache
from checkov.common.shared_graph.config import SharedGraphConfig
from checkov.common.shared_graph.service import SharedGraphService


def _write_repo(root: Path, file_count: int) -> list[str]:
    files: list[str] = []
    shared = root / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    policy = shared / "policy.yaml"
    policy.write_text(
        "Resources:\n"
        "  BasePolicy:\n"
        "    Type: AWS::IAM::Policy\n"
        "    Properties:\n"
        "      PolicyDocument:\n"
        "        Statement:\n"
        "          - Effect: Allow\n"
        '            Action: "*"\n'
    )
    files.append(str(policy))
    for index in range(file_count):
        cfn_path = root / f"stack_{index}.yaml"
        public_line = "      AccessControl: PublicRead\n" if index % 2 == 0 else ""
        cfn_path.write_text(
            "Resources:\n"
            f"  Bucket{index}:\n"
            "    Type: AWS::S3::Bucket\n"
            "    Properties:\n"
            f"{public_line}"
            "      Policy: '!ImportValue shared/policy.yaml#BasePolicy'\n"
        )
        generic_path = root / f"app_{index}.yaml"
        wildcard_line = "    wildcard: true\n" if index % 3 == 0 else ""
        generic_path.write_text(
            "resources:\n"
            f"  - name: app_bucket_{index}\n"
            "    type: bucket\n"
            "    policy_ref: '$ref: shared/policy.yaml#BasePolicy'\n"
            f"{wildcard_line}"
        )
        files.extend([str(cfn_path), str(generic_path)])
    return files


def _finding_keys(outcome_findings: dict) -> set[tuple[str, str, str, str, str]]:
    return {
        (
            finding.framework,
            finding.source_file,
            finding.resource_id,
            finding.native_type,
            finding.check_id,
        )
        for framework_result in outcome_findings.values()
        for finding in framework_result.findings
    }


def _measure(callable_):
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    result = callable_()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, elapsed, peak


def run_benchmark(file_count: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix="shared-graph-bench-"))
    cache_dir = Path(tempfile.mkdtemp(prefix="shared-graph-cache-"))
    try:
        files = _write_repo(root, file_count)
        adapters = list(DEFAULT_ADAPTERS)

        baseline = PerFrameworkBaseline()
        baseline_result, baseline_seconds, baseline_peak = _measure(
            lambda: baseline.run(files, adapters)
        )

        service = SharedGraphService(SharedGraphConfig(enabled=True))
        shared_outcome, shared_seconds, shared_peak = _measure(lambda: service.scan(files))
        build_seconds = shared_outcome.timings.graph_build_seconds
        check_seconds = shared_outcome.timings.check_seconds

        # Mutate one file and measure the incremental cost.
        changed = root / "stack_0.yaml"
        time.sleep(0.01)
        changed.write_text(
            "Resources:\n"
            "  Bucket0:\n"
            "    Type: AWS::S3::Bucket\n"
            "    Properties:\n"
            "      BucketEncryption:\n"
            "        SSEConfiguration: []\n"
            "      Policy: '!ImportValue shared/policy.yaml#BasePolicy'\n"
        )
        os.utime(changed, None)
        incremental_outcome, incremental_seconds, incremental_peak = _measure(
            lambda: service.apply_changes(changed_files=[str(changed)])
        )

        # Cold cache build then warm cache load in a fresh service.
        cache = GraphCache(cache_dir / "graph.json")
        service.save_cache(str(cache_dir / "graph.json"))
        warm_service = SharedGraphService(SharedGraphConfig(enabled=True), cache=cache)
        warm_outcome, cache_seconds, _ = _measure(lambda: warm_service.scan(files))

        shared_keys = _finding_keys(shared_outcome.findings_by_framework)
        baseline_keys = {
            (
                finding.framework,
                finding.source_file,
                finding.resource_id,
                finding.native_type,
                finding.check_id,
            )
            for finding in baseline_result.findings
        }

        per_framework = {
            framework: sorted(
                finding.check_id
                for finding in baseline_result.findings
                if finding.framework == framework
            )
            for framework in {finding.framework for finding in baseline_result.findings}
        }
        shared = {
            framework: sorted(finding.check_id for finding in result.findings)
            for framework, result in shared_outcome.findings_by_framework.items()
        }

        return {
            "file_count": len(files),
            "resource_files": file_count,
            "parse_calls": {
                "per_framework": baseline.parse_calls,
                "shared_once": len(files),
            },
            "parity": shared_keys == baseline_keys,
            "findings_only_in_baseline": sorted(baseline_keys - shared_keys),
            "findings_only_in_shared": sorted(shared_keys - baseline_keys),
            "per_framework_findings": per_framework,
            "shared_findings": shared,
            "seconds": {
                "per_framework_total": round(baseline_seconds, 4),
                "shared_total": round(shared_seconds, 4),
                "graph_build_once": round(build_seconds, 4),
                "shared_checks": round(check_seconds, 4),
                "incremental_one_file": round(incremental_seconds, 4),
                "incremental_reported": round(incremental_outcome.timings.incremental_seconds, 4),
                "warm_cache_scan": round(cache_seconds, 4),
            },
            "memory_peak_bytes": {
                "per_framework": baseline_peak,
                "shared": shared_peak,
                "incremental": incremental_peak,
            },
            "graph": {
                "nodes": len(shared_outcome.graph.nodes),
                "edges": len(shared_outcome.graph.edges),
                "placeholder_nodes": sum(
                    1 for node in shared_outcome.graph.nodes.values() if node.status.value == "placeholder"
                ),
                "approx_graph_bytes": shared_outcome.graph.approx_memory_bytes(),
            },
            "incremental": {
                "was_incremental": incremental_outcome.incremental,
                "rebuilt": incremental_outcome.rebuilt,
            },
            "cache": {
                "warm_scan_used_cache": warm_outcome.used_cache,
            },
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(cache_dir, ignore_errors=True)


def _print_report(result: dict) -> None:
    print("\n=== Shared graph acceptance benchmark ===")
    print(f"scanned files: {result['file_count']} "
          f"({result['graph']['nodes']} nodes, {result['graph']['edges']} edges, "
          f"{result['graph']['placeholder_nodes']} placeholders)")
    print("\n-- time (seconds) --")
    for name, value in result["seconds"].items():
        print(f"  {name:24s} {value:10.4f}")
    print("\n-- memory peak (MiB) --")
    for name, value in result["memory_peak_bytes"].items():
        print(f"  {name:24s} {value / 1024 / 1024:10.2f}")
    print("\n-- detection parity --")
    print(f"  identical findings: {result['parity']}")
    for framework in sorted(result["per_framework_findings"]):
        baseline = result["per_framework_findings"][framework]
        shared = result["shared_findings"].get(framework, [])
        print(f"  {framework:16s} baseline={len(baseline):4d} shared={len(shared):4d} match={baseline == shared}")
    print(f"\n  incremental path used: {result['incremental']['was_incremental']} "
          f"(full rebuild={result['incremental']['rebuilt']})")
    print(f"  warm cache reused:     {result['cache']['warm_scan_used_cache']}")


def main() -> int:
    file_count = int(os.getenv("CKV_BENCH_FILES", "800"))
    result = run_benchmark(file_count)
    _print_report(result)
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)
    output = artifacts / "shared_graph_benchmark.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nJSON artifact: {output}")
    if not result["parity"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

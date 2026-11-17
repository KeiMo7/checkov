---
layout: default
published: true
title: Scan Observability
nav_order: 20
---

# Scan Observability (progress, structured errors, cancellation)

Checkov can report runtime observability for long-running scans: throttled progress callbacks,
structured errors with a stable category, and cooperative cancellation. All of it is **opt-in** —
when the new parameters are not passed, return values, exception types and log output are
identical to previous versions.

## Enabling observability

`RunnerRegistry.run()` accepts two new optional keyword arguments (both default to `None`):

```python
from checkov.common.output.observability import CancellationToken, ProgressTracker
from checkov.common.runners.runner_registry import RunnerRegistry

tracker = ProgressTracker(interval_seconds=1.0)   # callback frequency is configurable
token = CancellationToken()

tracker.add_callback(lambda event: print(event.source, event.current, event.total))

reports = runner_registry.run(
    root_folder="./iac",
    progress_tracker=tracker,
    cancellation_token=token,
)
```

## Migration example

Before (unchanged, still works exactly the same):

```python
reports = runner_registry.run(root_folder="./iac")
```

After (observability enabled):

```python
from checkov.common.output.observability import (
    CancellationToken, ProgressTracker, ScanCancelledError, ScanError,
)

tracker = ProgressTracker(interval_seconds=2.0)
tracker.add_callback(lambda e: metrics.gauge("scan.progress", e.current, tags={"dir": e.source}))
token = CancellationToken()

try:
    reports = runner_registry.run(root_folder="./iac",
                                  progress_tracker=tracker,
                                  cancellation_token=token)
except ScanCancelledError:
    ...  # the caller cancelled; not a failure
except ScanError as exc:
    ...  # exc.category (parsing/dependency/configuration/internal/unknown) + exc.context
```

## Progress events

Each callback receives a `ProgressEvent(source, current, total, message, timestamp)`:

- `total` is `None` when the total is unknown (e.g. streaming input) — progress still flows.
- Callbacks are throttled by `interval_seconds`; the first and the completion update always fire.
- A callback that raises is logged (sanitized) and ignored — it never breaks the scan.
- The tracker keeps only the latest state per source, so memory stays flat on long runs.

For parallel scans of multiple directories, use `DirectoryProgressAggregator`:

```python
from checkov.common.output.observability import DirectoryProgressAggregator

aggregator = DirectoryProgressAggregator(interval_seconds=1.0)
aggregator.add_callback(on_event)
# per directory, in each worker:
handle = aggregator.tracker_for("./dir-a")
handle.update(current=3, total=10)
aggregator.snapshot()  # {"directories": {...}, "overall": {"current": ..., "total": ...}}
```

## Structured errors and cancellation

With observability enabled, failures are raised as `ScanError` with a `category`
(`ErrorCategory`) and a sanitized `context` dict. Cancellation raises `ScanCancelledError`
(category `cancelled`), which is distinct from a failure. `CancellationToken.wait()` acts as an
interruptible sleep so blocking loops respond to cancellation promptly.

Missing or outdated external tools can be checked with
`checkov.common.output.observability.ensure_dependency()`, which raises a readable
`ScanError(category=dependency)` instead of an `AttributeError`.

## Log hygiene

Messages and context values that go into logs or exceptions are sanitized with the existing
secret-detection policy: credentials are masked and absolute paths are redacted
(`<redacted-path:name>`).

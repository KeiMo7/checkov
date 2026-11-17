from __future__ import annotations

import inspect
import os
import unittest

os.environ["CHECKOV_PARALLELIZATION_TYPE"] = "none"

from checkov.common.output.observability import (  # noqa: E402
    CancellationToken,
    ErrorCategory,
    ProgressEvent,
    ProgressTracker,
    ScanCancelledError,
    ScanError,
)
from checkov.common.output.report import Report  # noqa: E402
from checkov.common.runners.base_runner import BaseRunner  # noqa: E402
from checkov.common.runners.runner_registry import RunnerRegistry  # noqa: E402
from checkov.runner_filter import RunnerFilter  # noqa: E402


class DummyRunner(BaseRunner):
    check_type = "terraform"

    def __init__(self, check_type: str = "terraform", fail_with: Exception | None = None):
        super().__init__()
        self.check_type = check_type
        self.fail_with = fail_with

    def run(self, root_folder, external_checks_dir=None, files=None, runner_filter=None, collect_skip_comments=True):
        if self.fail_with is not None:
            raise self.fail_with
        return Report(self.check_type)


def build_registry(*runners: DummyRunner) -> RunnerRegistry:
    return RunnerRegistry("banner", RunnerFilter(), *runners)


class TestDefaultBehaviorUnchanged(unittest.TestCase):
    def test_new_parameters_default_to_off(self):
        signature = inspect.signature(RunnerRegistry.run)
        self.assertIsNone(signature.parameters["progress_tracker"].default)
        self.assertIsNone(signature.parameters["cancellation_token"].default)

    def test_run_without_observability_returns_reports(self):
        registry = build_registry(DummyRunner())
        reports = registry.run(root_folder="/tmp")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].check_type, "terraform")

    def test_failure_without_observability_raises_original_exception(self):
        registry = build_registry(DummyRunner(fail_with=ValueError("bad config")))
        with self.assertRaises(ValueError):
            registry.run(root_folder="/tmp")


class TestObservableRun(unittest.TestCase):
    def test_progress_events_and_lifecycle_logs(self):
        registry = build_registry(DummyRunner("terraform"), DummyRunner("cloudformation"))
        tracker = ProgressTracker(interval_seconds=0)
        events: list[ProgressEvent] = []
        tracker.add_callback(events.append)

        logger_name = "checkov.common.runners.runner_registry"
        with self.assertLogs(logger_name, level="INFO") as logs:
            reports = registry.run(root_folder="/tmp", progress_tracker=tracker)

        self.assertEqual(len(reports), 2)
        self.assertEqual(len(events), 2)
        self.assertEqual([event.current for event in events], [1, 2])
        self.assertTrue(all(event.total == 2 for event in events))
        self.assertTrue(any("Starting scan run" in line for line in logs.output))
        self.assertTrue(any("Completed scan run" in line for line in logs.output))

    def test_cancellation_raises_cancelled_not_failure(self):
        registry = build_registry(DummyRunner())
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(ScanCancelledError) as ctx:
            registry.run(root_folder="/tmp", cancellation_token=token)
        self.assertEqual(ctx.exception.category, ErrorCategory.CANCELLED)

    def test_failure_is_wrapped_with_category_and_context(self):
        registry = build_registry(DummyRunner(fail_with=ValueError("bad config")))
        logger_name = "checkov.common.runners.runner_registry"
        with self.assertLogs(logger_name, level="ERROR") as logs:
            with self.assertRaises(ScanError) as ctx:
                registry.run(root_folder="/tmp", progress_tracker=ProgressTracker())
        self.assertEqual(ctx.exception.category, ErrorCategory.CONFIGURATION)
        self.assertIn("bad config", str(ctx.exception))
        self.assertTrue(any("failed" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()

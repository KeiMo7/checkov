from __future__ import annotations

import json
import time
import unittest

from checkov.common.output.observability import (
    DEFAULT_PROGRESS_INTERVAL_SECONDS,
    CancellationToken,
    DirectoryProgressAggregator,
    ErrorCategory,
    ProgressEvent,
    ProgressTracker,
    ScanCancelledError,
    ScanError,
    ensure_dependency,
    sanitize_context,
    sanitize_text,
)


class TestProgressTracker(unittest.TestCase):
    def test_long_task_reports_at_least_three_increasing_updates(self):
        tracker = ProgressTracker(interval_seconds=0.05)
        events: list[ProgressEvent] = []
        tracker.add_callback(events.append)

        total = 20
        for index in range(1, total + 1):
            time.sleep(0.02)  # simulate a unit of work
            tracker.update("scan", index, total)
        tracker.finish("scan")

        self.assertGreaterEqual(len(events), 3)
        currents = [event.current for event in events]
        self.assertEqual(currents, sorted(currents))
        self.assertEqual(len(set(currents)), len(currents), "progress values must be strictly increasing")
        self.assertEqual(currents[-1], total)
        self.assertTrue(all(event.total == total for event in events))

    def test_unknown_total_still_emits_progress(self):
        tracker = ProgressTracker(interval_seconds=0)
        events: list[ProgressEvent] = []
        tracker.add_callback(events.append)

        for index in range(1, 4):
            tracker.update("stream", index, None)

        self.assertEqual(len(events), 3)
        self.assertTrue(all(event.total is None for event in events))
        self.assertEqual([event.current for event in events], [1, 2, 3])

    def test_throttling_limits_callback_frequency(self):
        tracker = ProgressTracker(interval_seconds=60)
        events: list[ProgressEvent] = []
        tracker.add_callback(events.append)

        for index in range(1, 101):
            tracker.update("scan", index, 100)

        # first update + completion update only, regardless of the 100 updates
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1].current, 100)

    def test_callback_exception_does_not_break_main_flow(self):
        tracker = ProgressTracker(interval_seconds=0)
        good_events: list[ProgressEvent] = []

        def broken_callback(event: ProgressEvent) -> None:
            raise RuntimeError("boom /home/user/secret/path")

        tracker.add_callback(broken_callback)
        tracker.add_callback(good_events.append)

        with self.assertLogs("checkov.common.output.observability", level="WARNING") as logs:
            for index in range(1, 6):
                tracker.update("scan", index, 5)

        self.assertEqual(len(good_events), 5, "main flow must continue despite callback errors")
        self.assertEqual(tracker.callback_error_count, 5)
        self.assertTrue(any("ignored" in line for line in logs.output))
        self.assertFalse(any("/home/user/secret/path" in line for line in logs.output),
                         "callback error logs must be sanitized")

    def test_state_is_bounded_on_long_runs(self):
        tracker = ProgressTracker(interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS)
        for index in range(10_000):
            tracker.update("scan", index, None)
        self.assertEqual(len(tracker.snapshot()), 1)


class TestStructuredErrors(unittest.TestCase):
    def test_failing_input_produces_category_and_context(self):
        raw_error: Exception
        try:
            json.loads("{not valid json")
        except Exception as exc:  # noqa: BLE001
            raw_error = exc

        scan_error = ScanError.from_exception(raw_error, context={"file": "main.tf", "line": 3})

        self.assertEqual(scan_error.category, ErrorCategory.PARSING)
        self.assertEqual(scan_error.context["file"], "main.tf")
        self.assertEqual(scan_error.context["line"], 3)
        self.assertIn("parsing", str(scan_error))
        self.assertEqual(scan_error.to_dict()["category"], "parsing")

    def test_missing_dependency_error_is_readable(self):
        with self.assertRaises(ScanError) as ctx:
            ensure_dependency("definitely-missing-tool-xyz")
        self.assertEqual(ctx.exception.category, ErrorCategory.DEPENDENCY)
        self.assertIn("definitely-missing-tool-xyz", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, AttributeError)

    def test_outdated_dependency_error_is_readable(self):
        with self.assertRaises(ScanError) as ctx:
            ensure_dependency("python3", min_version="99.0.0")
        self.assertEqual(ctx.exception.category, ErrorCategory.DEPENDENCY)
        self.assertIn("99.0.0", str(ctx.exception))

    def test_cancellation_is_distinguished_from_failure(self):
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(ScanCancelledError) as ctx:
            token.throw_if_cancelled({"source": "dir-a"})
        self.assertIsInstance(ctx.exception, ScanError)
        self.assertEqual(ctx.exception.category, ErrorCategory.CANCELLED)

        failure = ScanError.from_exception(ValueError("bad config"))
        self.assertNotIsInstance(failure, ScanCancelledError)
        self.assertNotEqual(failure.category, ErrorCategory.CANCELLED)

    def test_cancellation_token_wait_is_interruptible(self):
        token = CancellationToken()
        start = time.monotonic()
        token.cancel()
        self.assertTrue(token.wait(30))
        self.assertLess(time.monotonic() - start, 5)


class TestSanitization(unittest.TestCase):
    def test_error_and_logs_do_not_leak_credentials_or_absolute_paths(self):
        secret_key = "AKIAIOSFODNN7EXAMPLE"
        secret_value = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        absolute_path = "/home/someuser/private/project/main.tf"
        message = f"failed reading {absolute_path} with aws_access_key_id={secret_key} token={secret_value}"
        context = {"file": absolute_path, "password": "hunter2", "aws_token": secret_value}

        import logging
        logger = logging.getLogger("checkov.common.output.observability")
        with self.assertLogs(logger, level="WARNING") as logs:
            error = ScanError(message, ErrorCategory.PARSING, context)
            logger.warning("scan failed: %s", error)

        rendered = str(error) + repr(error.to_dict()) + "".join(logs.output)
        self.assertNotIn(secret_key, rendered)
        self.assertNotIn(secret_value, rendered)
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn(absolute_path, rendered)
        self.assertNotIn("/home/someuser", rendered)

    def test_sanitize_helpers(self):
        self.assertNotIn("/home/user", sanitize_text("error in /home/user/x.tf"))
        sanitized = sanitize_context({"token": "abc", "nested": {"path": "/etc/passwd"}})
        self.assertEqual(sanitized["token"], "*" * 10)
        self.assertNotIn("/etc/passwd", sanitized["nested"]["path"])


class TestDirectoryProgressAggregator(unittest.TestCase):
    def test_progress_is_grouped_per_directory(self):
        aggregator = DirectoryProgressAggregator(interval_seconds=0)
        events: list[ProgressEvent] = []
        aggregator.add_callback(events.append)

        dir_a = aggregator.tracker_for("dir-a")
        dir_b = aggregator.tracker_for("dir-b")
        dir_a.update(1, 2)
        dir_b.update(1, 4)
        dir_a.update(2, 2)
        dir_b.update(4, 4)

        snapshot = aggregator.snapshot()
        self.assertEqual(snapshot["directories"]["dir-a"], {"current": 2, "total": 2, "message": ""})
        self.assertEqual(snapshot["directories"]["dir-b"]["current"], 4)
        self.assertEqual(snapshot["overall"], {"current": 6, "total": 6})
        self.assertEqual({event.source for event in events}, {"dir-a", "dir-b"})

    def test_overall_total_unknown_when_any_directory_total_unknown(self):
        aggregator = DirectoryProgressAggregator(interval_seconds=0)
        aggregator.tracker_for("dir-a").update(1, 2)
        aggregator.tracker_for("dir-b").update(1, None)
        self.assertIsNone(aggregator.snapshot()["overall"]["total"])


if __name__ == "__main__":
    unittest.main()

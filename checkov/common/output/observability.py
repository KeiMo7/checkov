from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PROGRESS_INTERVAL_SECONDS = 1.0
_OBFUSCATION_LENGTH = 10
_OBFUSCATION = "*" * _OBFUSCATION_LENGTH


class ErrorCategory(str, Enum):
    PARSING = "parsing"
    DEPENDENCY = "dependency"
    CONFIGURATION = "configuration"
    CANCELLED = "cancelled"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w.:-])(?:/[^\s/:'\"(),]+)+/?")
_WINDOWS_PATH_PATTERN = re.compile(r"\b[A-Za-z]:\\(?:[^\s\\:'\"(),]+\\?)+")
_CREDENTIAL_KV_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|credential)"
    r"\s*(\"|')?\s*[:=]\s*(\"|')?([^\s\"',)]+)"
)
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
)


def _secrets_regex_patterns() -> list[str]:
    """Reuse the existing secret-detection policy, loaded lazily so this module stays dependency-light."""
    try:
        from checkov.common.util.secrets import _secrets_regexes
        return [pattern for patterns in _secrets_regexes.values() for pattern in patterns]
    except Exception:
        return []


def sanitize_text(text: str) -> str:
    """Remove credentials and absolute paths from a string before it is logged or raised."""
    if not text or not isinstance(text, str):
        return text

    sanitized = _CREDENTIAL_KV_PATTERN.sub(lambda m: f"{m.group(1)}={_OBFUSCATION}", text)
    for pattern in _secrets_regex_patterns():
        try:
            sanitized = re.sub(pattern, _OBFUSCATION, sanitized)
        except re.error:
            continue
    sanitized = _WINDOWS_PATH_PATTERN.sub("<redacted-path>", sanitized)
    sanitized = _ABSOLUTE_PATH_PATTERN.sub(
        lambda m: f"<redacted-path:{os.path.basename(m.group(0).rstrip('/')) or 'root'}>",
        sanitized,
    )
    return sanitized


def sanitize_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively sanitize string values of a context mapping."""
    sanitized: dict[str, Any] = {}
    for key, value in context.items():
        if isinstance(value, str) and _SENSITIVE_KEY_PATTERN.search(str(key)):
            sanitized[key] = _OBFUSCATION
        elif isinstance(value, str):
            sanitized[key] = sanitize_text(value)
        elif isinstance(value, Mapping):
            sanitized[key] = sanitize_context(value)
        elif isinstance(value, (list, tuple)):
            sanitized[key] = [sanitize_text(item) if isinstance(item, str) else item for item in value]
        else:
            sanitized[key] = value
    return sanitized


# ---------------------------------------------------------------------------
# Structured errors
# ---------------------------------------------------------------------------

class ScanError(Exception):
    """A failure with a stable category and sanitized key context, instead of raw output."""

    def __init__(
        self,
        message: str,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.category = category
        self.context = sanitize_context(context) if context else {}
        self.message = sanitize_text(message)
        super().__init__(self.message)

    def __str__(self) -> str:
        base = f"[{self.category.value}] {self.message}"
        if self.context:
            details = ", ".join(f"{key}={value}" for key, value in self.context.items())
            return f"{base} ({details})"
        return base

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category.value, "message": self.message, "context": dict(self.context)}

    @classmethod
    def from_exception(cls, exc: BaseException, context: Mapping[str, Any] | None = None) -> ScanError:
        if isinstance(exc, ScanError):
            return exc
        return cls(message=str(exc) or exc.__class__.__name__, category=classify_exception(exc), context=context)


class ScanCancelledError(ScanError):
    """Raised when a scan is cancelled. Distinct from a failure so callers can tell them apart."""

    def __init__(self, message: str = "Scan was cancelled", context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, ErrorCategory.CANCELLED, context)


def classify_exception(exc: BaseException) -> ErrorCategory:
    if isinstance(exc, ScanError):
        return exc.category
    if isinstance(exc, (ImportError, ModuleNotFoundError, FileNotFoundError)):
        return ErrorCategory.DEPENDENCY
    module = type(exc).__module__ or ""
    if "yaml" in module or "json" in module or "parser" in module.lower():
        return ErrorCategory.PARSING
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return ErrorCategory.CONFIGURATION
    return ErrorCategory.UNKNOWN


def ensure_dependency(
    executable: str,
    min_version: str | None = None,
    version_args: tuple[str, ...] = ("--version",),
    timeout_seconds: float = 10.0,
) -> str:
    """Verify an external tool exists (and is new enough), raising a readable ScanError otherwise."""
    path = shutil.which(executable)
    if path is None:
        raise ScanError(
            f"Required tool '{executable}' is not installed or not on PATH",
            ErrorCategory.DEPENDENCY,
            {"tool": executable},
        )
    if min_version is not None:
        try:
            output = subprocess.run(
                [path, *version_args], capture_output=True, text=True, timeout=timeout_seconds, check=False
            )
            version_text = f"{output.stdout}\n{output.stderr}"
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScanError(
                f"Could not determine version of tool '{executable}': {exc}",
                ErrorCategory.DEPENDENCY,
                {"tool": executable},
            ) from exc
        found = re.search(r"\d+(?:\.\d+)*", version_text)
        if found is None:
            raise ScanError(
                f"Could not parse version of tool '{executable}'",
                ErrorCategory.DEPENDENCY,
                {"tool": executable},
            )
        found_parts = tuple(int(part) for part in found.group(0).split("."))
        required_parts = tuple(int(part) for part in min_version.split("."))
        length = max(len(found_parts), len(required_parts))
        if found_parts + (0,) * (length - len(found_parts)) < required_parts + (0,) * (length - len(required_parts)):
            raise ScanError(
                f"Tool '{executable}' version {found.group(0)} is older than required {min_version}",
                ErrorCategory.DEPENDENCY,
                {"tool": executable},
            )
    return path


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

class CancellationToken:
    """Cooperative cancellation. ``wait`` doubles as an interruptible sleep so blocking loops stay responsive."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self, context: Mapping[str, Any] | None = None) -> None:
        if self.is_cancelled:
            raise ScanCancelledError(context=context)

    def wait(self, timeout_seconds: float) -> bool:
        """Sleep up to ``timeout_seconds``; returns True immediately if cancelled meanwhile."""
        return self._event.wait(timeout_seconds)


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProgressEvent:
    source: str
    current: int
    total: int | None  # None when the total is unknown (e.g. streaming input)
    message: str
    timestamp: float


ProgressCallback = Callable[[ProgressEvent], None]


class ProgressTracker:
    """Throttled, exception-safe progress dispatch.

    State is kept per source only (no event history), so memory stays flat on long runs.
    Callback exceptions are logged and counted, never propagated.
    """

    def __init__(self, interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS) -> None:
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0")
        self.interval_seconds = interval_seconds
        self._callbacks: list[ProgressCallback] = []
        self._states: dict[str, ProgressEvent] = {}
        self._last_emit_monotonic: dict[str, float] = {}
        self._last_emitted: dict[str, tuple[int, int | None]] = {}
        self._lock = threading.Lock()
        self.callback_error_count = 0

    def add_callback(self, callback: ProgressCallback) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def remove_callback(self, callback: ProgressCallback) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def update(self, source: str, current: int, total: int | None = None, message: str = "", force: bool = False) -> None:
        event = ProgressEvent(source=source, current=current, total=total, message=message, timestamp=time.time())
        with self._lock:
            self._states[source] = event
            last_emit = self._last_emit_monotonic.get(source)
            now = time.monotonic()
            completed = total is not None and current >= total
            due = last_emit is None or (now - last_emit) >= self.interval_seconds
            if not (force or completed or due):
                return
            if self._last_emitted.get(source) == (current, total):
                return  # nothing new to report (e.g. finish() after the completion update)
            self._last_emit_monotonic[source] = now
            self._last_emitted[source] = (current, total)
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001 - a broken callback must not break the scan
                self.callback_error_count += 1
                logger.warning("Progress callback %r raised and was ignored: %s", getattr(callback, "__name__", callback), sanitize_text(str(exc)))

    def finish(self, source: str, message: str = "done") -> None:
        with self._lock:
            state = self._states.get(source)
        current = state.current if state else 0
        total = state.total if state else None
        if total is not None:
            current = max(current, total)
        self.update(source, current, total, message, force=True)

    def snapshot(self) -> dict[str, ProgressEvent]:
        with self._lock:
            return dict(self._states)


class DirectoryProgressAggregator:
    """Aggregates progress of parallel scans across multiple directories."""

    def __init__(self, interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS) -> None:
        self._tracker = ProgressTracker(interval_seconds=interval_seconds)

    def add_callback(self, callback: ProgressCallback) -> None:
        self._tracker.add_callback(callback)

    def tracker_for(self, directory: str) -> "DirectoryProgressHandle":
        return DirectoryProgressHandle(self._tracker, directory)

    def snapshot(self) -> dict[str, Any]:
        states = self._tracker.snapshot()
        per_directory = {
            source: {"current": event.current, "total": event.total, "message": event.message}
            for source, event in states.items()
        }
        overall_current = sum(event.current for event in states.values())
        totals = [event.total for event in states.values()]
        overall_total = sum(totals) if totals and all(total is not None for total in totals) else None
        return {"directories": per_directory, "overall": {"current": overall_current, "total": overall_total}}


class DirectoryProgressHandle:
    """Per-directory view of a shared tracker, so parallel scans report under their own key."""

    def __init__(self, tracker: ProgressTracker, directory: str) -> None:
        self._tracker = tracker
        self._directory = directory

    @property
    def directory(self) -> str:
        return self._directory

    def update(self, current: int, total: int | None = None, message: str = "", force: bool = False) -> None:
        self._tracker.update(self._directory, current, total, message, force)

    def finish(self, message: str = "done") -> None:
        self._tracker.finish(self._directory, message)

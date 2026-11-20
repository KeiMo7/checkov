from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class LabelMapping:
    """Bidirectional, configuration-driven mapping between framework labels.

    The shared graph only ever stores canonical labels. Framework adapters
    translate inbound resource labels through :meth:`to_canonical` while
    building the graph and restore their native vocabulary through
    :meth:`to_native` when producing reports.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._to_canonical: dict[str, dict[str, str]] = {}
        self._to_native: dict[str, dict[str, str]] = {}
        self._reference_patterns: dict[str, list[tuple[re.Pattern[str], ...]]] = {}
        if config:
            self.load_config(config)

    @classmethod
    def from_file(cls, mapping_file: str | Path) -> LabelMapping:
        path = Path(mapping_file)
        with path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        return cls(config)

    def load_config(self, config: dict[str, Any]) -> None:
        for framework, labels in (config.get("frameworks") or {}).items():
            self.register_framework(framework, labels)
        for framework, patterns in (config.get("reference_patterns") or {}).items():
            for pattern in patterns:
                self.register_reference_pattern(
                    framework, pattern["file"], pattern.get("name")
                )

    def register_framework(self, framework: str, native_to_canonical: dict[str, str]) -> None:
        """Register or replace a framework mapping without touching code."""

        forward = self._to_canonical.setdefault(framework, {})
        backward = self._to_native.setdefault(framework, {})
        for native, canonical in native_to_canonical.items():
            forward[native] = canonical
            # First native label registered for a canonical label wins, so the
            # reverse mapping stays deterministic even with aliases.
            backward.setdefault(canonical, native)
        logger.debug("registered %d labels for framework %s", len(native_to_canonical), framework)

    def to_canonical(self, framework: str, native_type: str) -> str:
        return self._to_canonical.get(framework, {}).get(native_type, native_type)

    def to_native(self, framework: str, canonical_type: str) -> str:
        return self._to_native.get(framework, {}).get(canonical_type, canonical_type)

    def known_frameworks(self) -> list[str]:
        return sorted(self._to_canonical)

    def register_reference_pattern(
        self, framework: str, file_pattern: str, name_pattern: str | None = None
    ) -> None:
        patterns = self._reference_patterns.setdefault(framework, [])
        compiled: list[re.Pattern[str]] = [re.compile(file_pattern)]
        if name_pattern:
            compiled.append(re.compile(name_pattern))
        patterns.append(tuple(compiled))  # type: ignore[arg-type]

    def reference_patterns(self, framework: str) -> list[tuple[re.Pattern[str], ...]]:
        return list(self._reference_patterns.get(framework, ()))

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class UpdateTrigger(str, Enum):
    """When an incremental update is applied.

    IMMEDIATE reacts to every file save (low latency, potentially redundant
    work); BATCHED coalesces a batch of changes until flushed (efficient, but
    the graph lags until the batch threshold or an explicit flush).
    """

    IMMEDIATE = "immediate"
    BATCHED = "batched"


class SharedGraphConfig:
    """Runtime configuration for the shared-graph subsystem.

    Defaults deliberately preserve today's behaviour: the shared graph is
    opt-in (``enabled=False``) and, unless explicitly enabled, every framework
    keeps parsing files independently.
    """

    DEFAULT_MAPPING_FILE = Path(__file__).with_name("mapping_config.yaml")

    def __init__(
        self,
        enabled: bool = False,
        mapping_file: str | Path | None = None,
        max_nodes: int = 200_000,
        max_cached_files: int = 100_000,
        update_trigger: UpdateTrigger = UpdateTrigger.IMMEDIATE,
        batched_threshold: int = 100,
        affected_node_cap: int = 10_000,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.enabled = enabled
        self.mapping_file = Path(mapping_file) if mapping_file else self.DEFAULT_MAPPING_FILE
        self.max_nodes = max_nodes
        self.max_cached_files = max_cached_files
        self.update_trigger = update_trigger
        self.batched_threshold = batched_threshold
        # Safety valve for incremental updates: a change touching more nodes
        # than this triggers a full rebuild instead of pathological fan-out.
        self.affected_node_cap = affected_node_cap
        self.extra = extra or {}

    @classmethod
    def from_env(cls) -> SharedGraphConfig:
        """Build a config from environment variables.

        ``CKV_SHARED_GRAPH`` is the single opt-in switch; everything else has
        a safe default so existing scans are byte-for-byte unchanged.
        """

        trigger = UpdateTrigger(
            os.getenv("CKV_SHARED_GRAPH_TRIGGER", UpdateTrigger.IMMEDIATE.value)
        )
        return cls(
            enabled=os.getenv("CKV_SHARED_GRAPH", "false").lower() in ("1", "true", "yes", "on"),
            mapping_file=os.getenv("CKV_SHARED_GRAPH_MAPPING") or cls.DEFAULT_MAPPING_FILE,
            max_nodes=int(os.getenv("CKV_SHARED_GRAPH_MAX_NODES", "200000")),
            max_cached_files=int(os.getenv("CKV_SHARED_GRAPH_MAX_FILES", "100000")),
            update_trigger=trigger,
            batched_threshold=int(os.getenv("CKV_SHARED_GRAPH_BATCH", "100")),
            affected_node_cap=int(os.getenv("CKV_SHARED_GRAPH_AFFECTED_CAP", "10000")),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> SharedGraphConfig:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        trigger_value = payload.pop("update_trigger", UpdateTrigger.IMMEDIATE.value)
        return cls(update_trigger=UpdateTrigger(trigger_value), **payload)

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import yaml

from checkov.common.shared_graph.models import FileFingerprint

logger = logging.getLogger(__name__)

YAML_EXTENSIONS = (".yaml", ".yml")
JSON_EXTENSIONS = (".json",)
SUPPORTED_EXTENSIONS = YAML_EXTENSIONS + JSON_EXTENSIONS


@dataclass
class ParsedDocument:
    """The single parse result shared by every framework for one file."""

    path: str
    documents: list[Any]
    fingerprint: FileFingerprint
    parse_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.parse_error is None


def fingerprint_file(path: str, compute_hash: bool = False) -> FileFingerprint:
    """Build a cheap (mtime, size) fingerprint; hash only when requested."""

    stat = os.stat(path)
    digest: str | None = None
    if compute_hash:
        digest = hash_file(path)
    return FileFingerprint(path=path, mtime_ns=stat.st_mtime_ns, size=stat.st_size, sha256=digest)


def hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_file(path: str) -> ParsedDocument:
    """Parse a YAML/JSON file exactly once.

    Failures never raise: the returned :class:`ParsedDocument` carries the
    error message so the graph builder can keep a placeholder node and the
    service can decide whether to degrade to per-framework parsing.
    """

    fingerprint = fingerprint_file(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError as error:
        return ParsedDocument(path=path, documents=[], fingerprint=fingerprint, parse_error=str(error))

    try:
        if path.endswith(JSON_EXTENSIONS):
            documents = [json.loads(content)]
        else:
            documents = [doc for doc in yaml.safe_load_all(content) if doc is not None]
    except (yaml.YAMLError, json.JSONDecodeError) as error:
        logger.warning("shared graph: failed to parse %s: %s", path, error)
        return ParsedDocument(path=path, documents=[], fingerprint=fingerprint, parse_error=str(error))

    return ParsedDocument(path=path, documents=documents, fingerprint=fingerprint)

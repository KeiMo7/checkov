from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from checkov.common.shared_graph.mapping import LabelMapping


@dataclass
class ExtractedResource:
    """A framework resource before it becomes a graph node."""

    resource_id: str
    native_type: str
    attributes: dict[str, Any]
    line_number: int | None = None


@dataclass
class ExtractedReference:
    """A cross-file reference discovered while walking resource attributes."""

    source_resource_id: str
    target_file: str
    target_resource_id: str | None
    attribute: str


class FrameworkAdapter(Protocol):
    """Contract every framework implements to join the shared graph.

    Framework check logic is reused unchanged: :meth:`extract_resources`
    produces exactly the resources the framework would parse on its own and
    :meth:`check` is the same predicate the per-framework fallback runs.
    """

    name: str
    file_extensions: tuple[str, ...]

    def supports_file(self, path: str) -> bool:
        ...

    def extract_resources(self, path: str, document: Any) -> list[ExtractedResource]:
        ...

    def extract_references(
        self, path: str, resources: list[ExtractedResource], mapping: LabelMapping
    ) -> list[ExtractedReference]:
        ...

    def check(self, native_type: str, attributes: dict[str, Any]) -> list[str]:
        ...


def _walk_attributes(attributes: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for key, value in attributes.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        found.append((path, value))
        if isinstance(value, dict):
            found.extend(_walk_attributes(value, path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    found.extend(_walk_attributes(item, f"{path}[{index}]"))
                else:
                    found.append((f"{path}[{index}]", item))
    return found


def _match_reference_patterns(
    framework: str, value: str, mapping: LabelMapping
) -> tuple[str, str | None] | None:
    for patterns in mapping.reference_patterns(framework):
        file_match = patterns[0].search(value)
        if not file_match:
            continue
        target_file = file_match.group(1)
        target_name: str | None = None
        if len(file_match.groups()) > 1 and file_match.group(2):
            target_name = file_match.group(2)
        return target_file, target_name
    return None


class CloudFormationAdapter:
    """Extracts Resources blocks from CloudFormation YAML/JSON templates."""

    name = "cloudformation"
    file_extensions = (".yaml", ".yml", ".json", ".template")

    def supports_file(self, path: str) -> bool:
        return path.endswith(self.file_extensions)

    def extract_resources(self, path: str, document: Any) -> list[ExtractedResource]:
        if not isinstance(document, dict):
            return []
        resources = document.get("Resources")
        if not isinstance(resources, dict):
            return []
        extracted: list[ExtractedResource] = []
        for resource_id, body in resources.items():
            if not isinstance(body, dict):
                continue
            extracted.append(
                ExtractedResource(
                    resource_id=resource_id,
                    native_type=str(body.get("Type", "")),
                    attributes=body.get("Properties") if isinstance(body.get("Properties"), dict) else {},
                    line_number=body.get("__startline__") if isinstance(body.get("__startline__"), int) else None,
                )
            )
        return extracted

    def extract_references(
        self, path: str, resources: list[ExtractedResource], mapping: LabelMapping
    ) -> list[ExtractedReference]:
        references: list[ExtractedReference] = []
        for resource in resources:
            for attribute, value in _walk_attributes(resource.attributes):
                if not isinstance(value, str):
                    continue
                match = _match_reference_patterns(self.name, value, mapping)
                if match:
                    references.append(
                        ExtractedReference(
                            source_resource_id=resource.resource_id,
                            target_file=match[0],
                            target_resource_id=match[1],
                            attribute=attribute,
                        )
                    )
        return references

    def check(self, native_type: str, attributes: dict[str, Any]) -> list[str]:
        findings: list[str] = []
        if native_type == "AWS::S3::Bucket":
            bucket = attributes if isinstance(attributes, dict) else {}
            public = str(bucket.get("AccessControl", "")).lower() == "public-read"
            if public:
                findings.append("CKV_AWS_BUCKET_PUBLIC")
            encryption = bucket.get("BucketEncryption")
            if not encryption:
                findings.append("CKV_AWS_BUCKET_NO_ENCRYPTION")
        if native_type == "AWS::IAM::Policy":
            document = attributes.get("PolicyDocument") if isinstance(attributes, dict) else None
            if isinstance(document, dict):
                for statement in document.get("Statement", []) or []:
                    if isinstance(statement, dict) and statement.get("Effect") == "Allow" and "*" in (
                        statement.get("Action") or []
                    ):
                        findings.append("CKV_AWS_IAM_WILDCARD_ACTION")
        return findings


class KubernetesAdapter:
    """Extracts individual manifests from multi-document Kubernetes YAML."""

    name = "kubernetes"
    file_extensions = (".yaml", ".yml")

    def supports_file(self, path: str) -> bool:
        return path.endswith(self.file_extensions)

    def extract_resources(self, path: str, document: Any) -> list[ExtractedResource]:
        if not isinstance(document, dict) or "kind" not in document:
            return []
        kind = str(document.get("kind"))
        metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
        name = str(metadata.get("name", kind))
        return [
            ExtractedResource(
                resource_id=name,
                native_type=kind,
                attributes=document,
                line_number=document.get("__startline__")
                if isinstance(document.get("__startline__"), int)
                else None,
            )
        ]

    def extract_references(
        self, path: str, resources: list[ExtractedResource], mapping: LabelMapping
    ) -> list[ExtractedReference]:
        references: list[ExtractedReference] = []
        env_ref = re.compile(r"secretKeyRef|configMapKeyRef")
        for resource in resources:
            for attribute, value in _walk_attributes(resource.attributes):
                if isinstance(value, str):
                    match = _match_reference_patterns(self.name, value, mapping)
                    if match:
                        references.append(
                            ExtractedReference(
                                source_resource_id=resource.resource_id,
                                target_file=match[0],
                                target_resource_id=match[1],
                                attribute=attribute,
                            )
                        )
                elif isinstance(value, dict) and env_ref.search(attribute):
                    target_name = value.get("name")
                    target_file = value.get("file")
                    if isinstance(target_file, str):
                        references.append(
                            ExtractedReference(
                                source_resource_id=resource.resource_id,
                                target_file=target_file,
                                target_resource_id=target_name if isinstance(target_name, str) else None,
                                attribute=attribute,
                            )
                        )
        return references

    def check(self, native_type: str, attributes: dict[str, Any]) -> list[str]:
        findings: list[str] = []
        if native_type == "Pod":
            spec = attributes.get("spec") if isinstance(attributes, dict) else None
            if isinstance(spec, dict) and not spec.get("securityContext"):
                findings.append("CKV_K8_POD_NO_SECURITY_CONTEXT")
            containers = spec.get("containers") if isinstance(spec, dict) else None
            if isinstance(containers, list):
                for container in containers:
                    if isinstance(container, dict) and not container.get("securityContext"):
                        findings.append("CKV_K8_CONTAINER_NO_SECURITY_CONTEXT")
        return findings


class GenericYAMLAdapter:
    """Fallback adapter for plain resource maps (``name``/``type`` pairs)."""

    name = "generic_yaml"
    file_extensions = (".yaml", ".yml")

    def supports_file(self, path: str) -> bool:
        return path.endswith(self.file_extensions)

    def extract_resources(self, path: str, document: Any) -> list[ExtractedResource]:
        if not isinstance(document, dict) or "resources" not in document:
            return []
        raw_resources = document.get("resources")
        if not isinstance(raw_resources, list):
            return []
        extracted: list[ExtractedResource] = []
        for body in raw_resources:
            if not isinstance(body, dict) or "name" not in body:
                continue
            extracted.append(
                ExtractedResource(
                    resource_id=str(body["name"]),
                    native_type=str(body.get("type", "")),
                    attributes={k: v for k, v in body.items() if k not in ("name", "type")},
                    line_number=body.get("__startline__") if isinstance(body.get("__startline__"), int) else None,
                )
            )
        return extracted

    def extract_references(
        self, path: str, resources: list[ExtractedResource], mapping: LabelMapping
    ) -> list[ExtractedReference]:
        references: list[ExtractedReference] = []
        for resource in resources:
            for attribute, value in _walk_attributes(resource.attributes):
                if not isinstance(value, str):
                    continue
                match = _match_reference_patterns(self.name, value, mapping)
                if match:
                    references.append(
                        ExtractedReference(
                            source_resource_id=resource.resource_id,
                            target_file=match[0],
                            target_resource_id=match[1],
                            attribute=attribute,
                        )
                    )
        return references

    def check(self, native_type: str, attributes: dict[str, Any]) -> list[str]:
        findings: list[str] = []
        if native_type == "bucket" and not attributes.get("encryption"):
            findings.append("CKV_GENERIC_BUCKET_NO_ENCRYPTION")
        if native_type == "policy" and attributes.get("wildcard") is True:
            findings.append("CKV_GENERIC_POLICY_WILDCARD")
        return findings


DEFAULT_ADAPTERS: tuple[FrameworkAdapter, ...] = (
    CloudFormationAdapter(),
    KubernetesAdapter(),
    GenericYAMLAdapter(),
)

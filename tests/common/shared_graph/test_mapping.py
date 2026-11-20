from __future__ import annotations

from checkov.common.shared_graph.config import SharedGraphConfig
from checkov.common.shared_graph.mapping import LabelMapping


def test_default_mapping_round_trips_for_every_framework() -> None:
    mapping = LabelMapping.from_file(SharedGraphConfig.DEFAULT_MAPPING_FILE)

    assert mapping.to_canonical("cloudformation", "AWS::S3::Bucket") == "storage.bucket"
    assert mapping.to_native("cloudformation", "storage.bucket") == "AWS::S3::Bucket"
    assert mapping.to_canonical("kubernetes", "Bucket") == "storage.bucket"
    assert mapping.to_native("generic_yaml", "storage.bucket") == "bucket"


def test_unknown_label_falls_back_to_itself() -> None:
    mapping = LabelMapping.from_file(SharedGraphConfig.DEFAULT_MAPPING_FILE)

    assert mapping.to_canonical("cloudformation", "AWS::New::Thing") == "AWS::New::Thing"
    assert mapping.to_native("cloudformation", "brand.canonical") == "brand.canonical"


def test_new_framework_registers_without_code_change(tmp_path) -> None:
    mapping = LabelMapping()
    mapping.register_framework("terra_frame", {"aws_s3_bucket": "storage.bucket"})

    assert mapping.to_canonical("terra_frame", "aws_s3_bucket") == "storage.bucket"
    assert mapping.to_native("terra_frame", "storage.bucket") == "aws_s3_bucket"
    assert "terra_frame" in mapping.known_frameworks()


def test_reference_patterns_are_framework_scoped() -> None:
    mapping = LabelMapping.from_file(SharedGraphConfig.DEFAULT_MAPPING_FILE)

    assert mapping.reference_patterns("cloudformation")
    assert mapping.reference_patterns("generic_yaml")
    assert mapping.reference_patterns("unknown") == []

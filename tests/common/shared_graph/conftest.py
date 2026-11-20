from __future__ import annotations

from pathlib import Path

import pytest


CFN_BUCKET = """
Resources:
  DataBucket:
    Type: AWS::S3::Bucket
    Properties:
      AccessControl: PublicRead
      Policy: '!ImportValue shared/policy.yaml#BasePolicy'
"""

CFN_POLICY = """
Resources:
  BasePolicy:
    Type: AWS::IAM::Policy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: "*"
"""

GENERIC_BUCKET = """
resources:
  - name: app_bucket
    type: bucket
    policy_ref: '$ref: shared/policy.yaml#BasePolicy'
"""


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "shared").mkdir()
    (tmp_path / "bucket.yaml").write_text(CFN_BUCKET)
    (tmp_path / "generic.yaml").write_text(GENERIC_BUCKET)
    (tmp_path / "shared" / "policy.yaml").write_text(CFN_POLICY)
    return tmp_path

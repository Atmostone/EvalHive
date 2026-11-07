"""Case-specific credentials are declared without exposing their values."""

from types import SimpleNamespace

import pytest
from evalhive_mcpmark.dataset import descriptor


@pytest.mark.parametrize(
    "service", ["filesystem", "postgres", "github", "notion", "playwright", "playwright_webarena"]
)
def test_service_credential_requirement_is_explicit_in_case_metadata(tmp_path, service):
    task = SimpleNamespace(
        name="fixture",
        category_id="fixture",
        task_id="fixture",
        task_instruction_path=tmp_path / "instruction.md",
        task_verification_path=tmp_path / "verify.py",
    )
    case = descriptor(
        task, tmp_path, service, "standard", "fixture-set", "original", "sha256:" + "a" * 64
    )
    assert ("credential.service_credentials" in case["requirements"]["capabilities"]) is (
        service in {"postgres", "github", "notion"}
    )

"""Regrade sufficiency requires typed provenance, retained bytes and evaluator-only bindings."""

from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
from app.benchmarks.regrade import verify_source
from evalhive_benchmark import ProtocolError


def inputs():
    artifact = {
        "id": "state",
        "uri": "artifact:fixture",
        "digest": "sha256:" + "a" * 64,
        "size_bytes": 4,
        "visibility": "private",
    }
    case = {
        "ref": {"dataset_id": "fixture", "revision": "1", "case_id": "saved"},
        "source_digest": "sha256:" + "b" * 64,
    }
    return {
        "evaluator": {"id": "fixture", "version": "1", "capabilities": {"regrade": "native"}},
        "spec": {
            "scope": {"workspace_id": "workspace", "trial_id": "trial"},
            "case": case,
            "config": {},
            "models": {role: {"credential_ref": role + "-key"} for role in ["agent", "judge"]},
            "credential_refs": {
                "agent-key": "private-agent-reference",
                "judge-key": "private-judge-reference",
            },
        },
        "environment_sessions": [{"plan_digest": "c" * 64}],
        "capture": {
            "artifacts": [artifact],
            "checkpoint": {
                "format_version": "1.0",
                "connector": {"id": "fixture", "version": "1"},
                "case": case["ref"],
                "source_digest": case["source_digest"],
                "state_schema": "native.saved.v1",
                "complete": True,
                "artifacts": [artifact],
                "evaluator_roles": ["judge"],
                "state": {"native": "opaque"},
            },
        },
    }


class Evidence:
    def __init__(self, missing=False):
        self.verified, self.missing = [], missing

    @asynccontextmanager
    async def open_verified(self, workspace, trial, artifact, **kwargs):
        assert (workspace, trial) == ("workspace", "trial")
        if self.missing:
            raise ProtocolError("ARTIFACT_READ_FAILED", "Fixture missing", "recording")
        self.verified.append(artifact)
        yield None


@pytest.mark.asyncio
async def test_verified_source_omits_agent_credentials_and_mutable_native_session():
    original = inputs()
    before = deepcopy(original)
    evidence = Evidence()
    result = await verify_source(evidence, "workspace", "trial", "evaluation", original)
    assert result["models"] == {"judge": original["spec"]["models"]["judge"]}
    assert result["credential_refs"] == {"judge-key": "private-judge-reference"}
    assert evidence.verified == original["capture"]["artifacts"] and original == before


@pytest.mark.parametrize(
    "change,code",
    [
        ("case", "CHECKPOINT_PROVENANCE"),
        ("digest", "CHECKPOINT_PROVENANCE"),
        ("connector", "CHECKPOINT_PROVENANCE"),
        ("incomplete", "CHECKPOINT_INCOMPLETE"),
        ("unsupported", "REGRADE_UNAVAILABLE"),
        ("legacy", "REGRADE_UNAVAILABLE"),
        ("uncaptured", "CHECKPOINT_CONFLICT"),
        ("duplicate", "CHECKPOINT_CONFLICT"),
        ("missing_model", "MODEL_BINDING_REQUIRED"),
        ("missing_key", "CREDENTIAL_REQUIRED"),
        ("agent", "INVALID_MESSAGE"),
        ("corrupt", "ARTIFACT_READ_FAILED"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_source_cannot_authorize_regrade(change, code):
    value = inputs()
    checkpoint = value["capture"]["checkpoint"]
    if change == "case":
        checkpoint["case"] = {**checkpoint["case"], "revision": "different"}
    if change == "digest":
        checkpoint["source_digest"] = "sha256:" + "d" * 64
    if change == "connector":
        checkpoint["connector"]["version"] = "2"
    if change == "incomplete":
        checkpoint["complete"] = False
    if change == "unsupported":
        value["evaluator"]["capabilities"]["regrade"] = "emulated"
    if change == "legacy":
        del value["capture"]["checkpoint"]
    if change == "uncaptured":
        value["capture"]["artifacts"] = []
    if change == "duplicate":
        checkpoint["artifacts"] *= 2
    if change == "missing_model":
        del value["spec"]["models"]["judge"]
    if change == "missing_key":
        del value["spec"]["credential_refs"]["judge-key"]
    if change == "agent":
        checkpoint["evaluator_roles"] = ["agent"]
    with pytest.raises(ProtocolError) as error:
        await verify_source(
            Evidence(missing=change == "corrupt"), "workspace", "trial", "evaluation", value
        )
    assert error.value.code == code

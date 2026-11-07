"""Registered PostgreSQL state and password storage stay inside one owned trial."""

import asyncio
from types import SimpleNamespace

import pytest
from evalhive_benchmark import ProtocolError, validate_environment
from evalhive_mcpmark.installation import declarations
from evalhive_mcpmark.native import NativeConfigurationError
from evalhive_mcpmark.task_client import launch_config
from evalhive_mcpmark.worker import MCPMarkConnector

IMAGE = "sha256:" + "a" * 64
RECEIPT = {"source_digest": "sha256:" + "b" * 64, "provenance": {"service": "postgres"}}


def test_postgres_registration_has_private_credentials_and_frozen_backup_mounts():
    resources, runtime = declarations(RECEIPT, IMAGE, IMAGE, postgres_image=IMAGE)
    assets = resources["assets"]
    assert assets["mcpmark.network"] == {"kind": "network", "egress": False}
    assert assets["mcpmark.postgres"]["user"] == "999:999"
    assert assets["mcpmark.postgres"]["storage_slots"]["data"]["no_copy"] is False
    assert (
        assets["mcpmark.postgres"]["environment"]["POSTGRES_PASSWORD_FILE"]
        == "/postgres-secret/password"
    )
    assert "POSTGRES_PASSWORD" not in assets["mcpmark.postgres"]["environment"]
    assert "secret" not in assets["mcpmark.task"]["storage_slots"]
    assert assets["mcpmark.world"]["storage_slots"]["secret"]["read_only"] is False
    assert assets["mcpmark.postgres"]["storage_slots"]["secret"]["read_only"] is True
    for name in ("world", "postgres"):
        assert {
            "file_id": "initial-state",
            "target": "/opt/native/postgres_state",
            "read_only": True,
        } in assets["mcpmark." + name]["mounts"]
    assert runtime["working_path"] == "/workspace"
    assert set(runtime["assets"]) == {
        "working",
        "connection",
        "world",
        "task",
        "network",
        "data",
        "secret",
        "postgres",
    }


def test_postgres_registration_requires_an_explicit_immutable_service_image():
    with pytest.raises(NativeConfigurationError, match="immutable"):
        declarations(RECEIPT, IMAGE, IMAGE)


def test_postgres_plan_has_owned_network_dependencies_and_requires_private_input(monkeypatch):
    _, bindings = declarations(RECEIPT, IMAGE, IMAGE, postgres_image=IMAGE)
    case = {
        "ref": {
            "dataset_id": "native-postgres",
            "revision": "verified",
            "case_id": "employees__fixture",
        },
        "title": "Native PostgreSQL fixture",
        "source_digest": RECEIPT["source_digest"],
        "native_ref": {"service": "postgres"},
        "requirements": {},
    }
    task = SimpleNamespace(service="postgres", category_id="employees")
    dataset = SimpleNamespace(
        receipt={
            **RECEIPT,
            "provenance": {**RECEIPT["provenance"], "initial_assets": [{"id": "postgres.backups"}]},
        },
        resolve=lambda selected: task,
        manager=SimpleNamespace(get_task_instruction=lambda selected: "Native public task"),
    )
    monkeypatch.setattr(
        "evalhive_mcpmark.worker.native_instruction", lambda: "Native public instruction"
    )
    worker = MCPMarkConnector(dataset=dataset, bindings=bindings)
    spec = {
        "protocol_version": "1.0",
        "scope": {"workspace_id": "workspace", "trial_id": "trial", "generation": 1},
        "case": case,
        "config": {},
        "models": {},
        "credential_refs": {"service_credentials": "fixture-reference"},
        "limits": {"wall_time_seconds": 600, "memory_mb": 3072, "cpu_cores": 2, "disk_mb": 4096},
    }
    asyncio.run(worker.handle("validate", {"spec": spec}, None))
    plan = asyncio.run(worker.handle("plan", {"spec": spec}, None))
    validate_environment(spec, plan)
    assert plan["requirements"]["capabilities"] == ["host.volume_copy_up_v1"]
    selected = {resource["id"]: resource for resource in plan["resources"]}
    assert selected["postgres"]["config"]["network_aliases"] == ["postgres"]
    assert "postgres" in selected["task"]["depends_on"]
    assert "secret" not in selected["task"]["config"]["storage"]
    assert selected["world"]["config"]["storage"]["secret"] == "secret"
    spec["credential_refs"] = {}
    with pytest.raises(ProtocolError, match="initial state"):
        asyncio.run(
            MCPMarkConnector(dataset=dataset, bindings=bindings).handle(
                "validate", {"spec": spec}, None
            )
        )


def test_postgres_client_keeps_its_password_out_of_argv_and_cannot_target_an_owner_database():
    config = {
        "host": "postgres",
        "port": 5432,
        "username": "postgres",
        "password": "fixture@only",
        "current_database": "mcpmark_fixture",
        "database_url": "unused-native-display-url",
    }
    argv, env = launch_config({"service": "postgres", "config": config})
    assert "fixture@only" not in str(argv)
    assert (
        env["DATABASE_URI"] == "postgresql://postgres:fixture%40only@postgres:5432/mcpmark_fixture"
    )
    with pytest.raises(ValueError, match="owned service"):
        launch_config(
            {"service": "postgres", "config": {**config, "host": "unregistered-database"}}
        )

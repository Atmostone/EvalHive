"""Public profile validation does not read private credential values."""

import uuid
from unittest.mock import AsyncMock

import pytest
from app.benchmarks import profiles


def test_inline_credentials_cannot_hide_in_nested_native_settings():
    profiles.reject_inline_credentials({"max_tokens": 32, "native": {"timeout": 1}})
    for config in [{"api_key": "private"}, {"native": [{"Authorization": "private"}]}]:
        with pytest.raises(profiles.ProfileError) as caught:
            profiles.reject_inline_credentials(config)
        assert "private" not in str(caught.value)


async def test_profile_configuration_presence_is_distinct_from_live_verification(monkeypatch):
    declaration = {"config_schema": {"type": "object", "additionalProperties": False},
        "secret_schema": {"type": "object", "properties": {"api_key": {"type": "string"}},
                          "required": ["api_key"], "additionalProperties": False},
        "requirements": {"capabilities": ["model.user"]}}
    monkeypatch.setattr(profiles, "get_manifest", lambda *args: declaration)
    workspace, secret, model = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = AsyncMock()
    missing = await profiles.validate_profile(db, workspace, connector_id="example", connector_version="1",
        config={}, secret_refs={}, model_bindings={})
    assert not missing["configured"] and missing["missing"] == ["model.user", "secret.api_key"]
    assert db.scalar.await_count == 0
    db.scalar.side_effect = [secret, model]
    present = await profiles.validate_profile(db, workspace, connector_id="example", connector_version="1",
        config={}, secret_refs={"api_key": str(secret)}, model_bindings={"user": str(model)})
    assert present["configured"]
    statements = [str(call.args[0]) for call in db.scalar.await_args_list]
    assert all("workspace_id" in sql for sql in statements)
    assert "benchmark_secrets.value" not in statements[0]
    assert "providers.api_key" not in statements[1]
    db.scalar.side_effect = [None]
    with pytest.raises(profiles.ProfileError, match="unavailable in this workspace"):
        await profiles.validate_profile(db, workspace, connector_id="example", connector_version="1",
            config={}, secret_refs={"api_key": str(secret)}, model_bindings={})

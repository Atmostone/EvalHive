"""Original external state managers receive explicit targets and mocked transports."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from evalhive_mcpmark.external_state import create_state, module, validate_settings
from evalhive_mcpmark.native import NativeConfigurationError


@pytest.fixture
def native_source():
    if not os.getenv("EVALHIVE_MCPMARK_NATIVE_ROOT"):
        pytest.skip("Requires the pinned native managers and isolated service dependencies")


@pytest.mark.parametrize("service", ["github", "notion"])
def test_external_targets_never_use_upstream_account_defaults(service):
    with pytest.raises(NativeConfigurationError, match="explicit"):
        validate_settings(service, {})


@pytest.mark.parametrize("organization_exists", [True, False])
def test_original_github_constructor_cannot_fall_back_to_the_authenticated_user(
    native_source, tmp_path, monkeypatch, organization_exists
):
    native = module("github")
    calls = []

    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, url):
            calls.append(url)
            status = 200 if url.endswith("/user") or organization_exists else 404
            return SimpleNamespace(
                status_code=status, text="fixture", json=lambda: {"login": "fixture-person"}
            )

    monkeypatch.setattr(native, "requests", SimpleNamespace(Session=Session))
    (tmp_path / "codecrafters-io-build-your-own-x").mkdir()

    def create():
        return create_state(
            "github",
            {"evaluation_owner": "dedicated-eval-fixture", "dedicated_target": True},
            {"github_token": "fixture-only-token"},
            category="build_your_own_x",
            initial_root=tmp_path,
            private_root=tmp_path / "private",
            effect=None,
        )

    if organization_exists:
        state = create()
        assert state.eval_org == "dedicated-eval-fixture"
        assert state.get_service_config_for_agent()["github_token"] == "fixture-only-token"
    else:
        with pytest.raises(NativeConfigurationError, match="fallback"):
            create()
    assert calls == [
        "https://api.github.com/user",
        "https://api.github.com/orgs/dedicated-eval-fixture",
    ]


def test_original_notion_manager_uses_separate_keys_and_private_browser_state(
    native_source, tmp_path, monkeypatch
):
    native = module("notion")
    monkeypatch.setattr(native, "Client", lambda auth: SimpleNamespace(auth=auth))
    monkeypatch.chdir(tmp_path)
    for key in (
        "SOURCE_NOTION_API_KEY",
        "EVAL_NOTION_API_KEY",
        "SOURCE_PARENT_PAGE_TITLE",
        "EVAL_PARENT_PAGE_TITLE",
        "PLAYWRIGHT_HEADLESS",
        "PLAYWRIGHT_BROWSER",
    ):
        monkeypatch.setenv(key, "fixture")
    import importlib

    registry = importlib.import_module("src.config.config_schema").ConfigRegistry
    monkeypatch.setattr(registry, "_instances", {})
    root = tmp_path / "private"
    state = create_state(
        "notion",
        {
            "source_hub": "Fixture Source",
            "evaluation_hub": "Fixture Evaluation",
            "dedicated_target": True,
        },
        {
            "source_key": "source-fixture",
            "evaluation_key": "eval-fixture",
            "browser_state": {"cookies": [], "origins": []},
        },
        category="fixture",
        initial_root=tmp_path,
        private_root=root,
        effect=None,
    )
    assert state.source_notion_client.auth == "source-fixture"
    assert state.eval_notion_client.auth == "eval-fixture"
    assert state.source_parent_page_title == "Fixture Source"
    assert state.eval_parent_page_title == "Fixture Evaluation"
    assert state.get_service_config_for_agent() == {"notion_key": "eval-fixture"}
    assert Path.cwd() == root
    assert state.state_file.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700


def test_original_webarena_uses_broker_transport_and_retains_global_native_defaults(
    native_source, tmp_path
):
    native = module("playwright_webarena")
    original = native.PlaywrightStateManager.CATEGORY_CONFIGS["shopping_admin"].copy()
    effects = []

    def effect(operation, payload):
        effects.append((operation, payload))
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    state = create_state(
        "playwright_webarena",
        {
            "site": {
                "image_alias": original["image_name"],
                "container_alias": original["container_name"],
                "port": 80,
            }
        },
        {},
        category="shopping_admin",
        initial_root=tmp_path,
        private_root=tmp_path / "private",
        effect=effect,
    )
    assert state.CATEGORY_CONFIGS["shopping_admin"]["host_port"] == 80
    assert native.PlaywrightStateManager.CATEGORY_CONFIGS["shopping_admin"] == original
    state._stop_and_remove_container(original["container_name"])
    assert effects[:2] == [
        ("webarena.command", {"argv": ["docker", "stop", original["container_name"]]}),
        ("webarena.command", {"argv": ["docker", "rm", original["container_name"]]}),
    ]

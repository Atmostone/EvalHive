"""Construct pinned external state managers with explicit dedicated target bindings."""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
from copy import deepcopy
from pathlib import Path

from .native import NativeConfigurationError, native_code


def module(service):
    native_code()
    return importlib.import_module(
        "src.mcp_services."
        + service
        + "."
        + (
            "github_state_manager"
            if service == "github"
            else "notion_state_manager"
            if service == "notion"
            else "playwright_state_manager"
        )
    )


def browser_options(settings, *, default="chromium"):
    browser = settings.get("browser", default)
    if browser not in {"chromium", "firefox"} or settings.get("headless", True) is not True:
        raise NativeConfigurationError("Native browser requires a supported headless engine")
    return {"browser": browser, "headless": True}


def github_template(category):
    root = native_code()
    tree = ast.parse((root / "src/mcp_services/github/github_state_manager.py").read_text())
    mappings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "initial_state_mapping"
            for target in node.targets
        )
    ]
    if len(mappings) != 1:
        raise NativeConfigurationError("Native GitHub template mapping is unavailable")
    manager = object.__new__(module("github").GitHubStateManager)
    manager.initial_state_mapping = ast.literal_eval(mappings[0])
    return manager.select_initial_state_for_task(category)


def validate_settings(service, settings):
    if not isinstance(settings, dict):
        raise NativeConfigurationError("Native external settings must be explicit")
    if service == "github":
        if (
            set(settings) != {"evaluation_owner", "dedicated_target"}
            or settings["dedicated_target"] is not True
            or not isinstance(settings["evaluation_owner"], str)
            or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", settings["evaluation_owner"]
            )
        ):
            raise NativeConfigurationError("GitHub requires an explicit dedicated evaluation owner")
    elif service == "notion":
        if (
            not {"source_hub", "evaluation_hub", "dedicated_target"} <= set(settings)
            or set(settings)
            - {"source_hub", "evaluation_hub", "dedicated_target", "browser", "headless"}
            or settings["dedicated_target"] is not True
            or any(
                not isinstance(settings[name], str) or not settings[name].strip()
                for name in ("source_hub", "evaluation_hub")
            )
        ):
            raise NativeConfigurationError(
                "Notion requires explicit dedicated source and evaluation hubs"
            )
        browser_options(settings)
    elif service in {"playwright", "playwright_webarena"}:
        allowed = {"browser", "headless"}
        if service == "playwright_webarena":
            if settings.get("browser", "chromium") != "chromium":
                raise NativeConfigurationError(
                    "The pinned native WebArena client requires Chromium"
                )
            allowed |= {"site", "readiness_timeout_seconds"}
            site = settings.get("site")
            if (
                not isinstance(site, dict)
                or set(site) != {"image_alias", "container_alias", "port"}
                or any(
                    not isinstance(site[name], str) or not site[name]
                    for name in ("image_alias", "container_alias")
                )
                or type(site["port"]) is not int
                or site["port"] != 80
                or type(settings.get("readiness_timeout_seconds", 600)) is not int
                or not 1 <= settings.get("readiness_timeout_seconds", 600) <= 1200
            ):
                raise NativeConfigurationError("WebArena requires one explicit owned site binding")
        if set(settings) - allowed:
            raise NativeConfigurationError("Native browser settings contain unsupported parameters")
        browser_options(settings)
    else:
        raise NativeConfigurationError("Native external service is unsupported")
    return deepcopy(settings)


def validate_credentials(service, private):
    if not isinstance(private, dict):
        raise NativeConfigurationError("Native external credentials are invalid")
    if service == "github":
        if (
            set(private) != {"github_token"}
            or not isinstance(private["github_token"], str)
            or not private["github_token"]
        ):
            raise NativeConfigurationError("GitHub requires a dedicated token reference")
    elif service == "notion":
        if (
            set(private) != {"source_key", "evaluation_key", "browser_state"}
            or any(
                not isinstance(private[name], str) or not private[name]
                for name in ("source_key", "evaluation_key")
            )
            or private["source_key"] == private["evaluation_key"]
            or not isinstance(private["browser_state"], dict)
        ):
            raise NativeConfigurationError(
                "Notion requires separate dedicated keys and browser state"
            )
    elif service not in {"playwright", "playwright_webarena"} or private:
        raise NativeConfigurationError("Native public browser services do not accept credentials")


def create_state(service, settings, private, *, category, initial_root, private_root, effect):
    settings = validate_settings(service, settings)
    validate_credentials(service, private)
    native = module(service)
    if service == "github":
        template = github_template(category)
        if not template or not (Path(initial_root) / template).is_dir():
            raise NativeConfigurationError("The native GitHub category template is not installed")
        state = native.GitHubStateManager(
            github_token=private["github_token"],
            eval_org=settings["evaluation_owner"],
            templates_root=str(initial_root),
        )
        # Upstream falls back to the authenticated user's account. No native
        # mutations may run after that changes the explicitly installed target.
        if state.eval_org != settings["evaluation_owner"]:
            raise NativeConfigurationError("GitHub evaluation owner fallback is not permitted")
        return state
    if service == "notion":
        root = Path(private_root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.resolve() != root or root.stat().st_mode & 0o077:
            raise NativeConfigurationError(
                "Native Notion browser state requires a private directory"
            )
        path = root / "notion_state.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(private["browser_state"], stream)
        # The upstream manager locates its browser state in the process cwd.
        # A world hosts one native service; its control paths are absolute.
        os.chdir(root)
        for key, value in {
            "SOURCE_NOTION_API_KEY": private["source_key"],
            "EVAL_NOTION_API_KEY": private["evaluation_key"],
            "SOURCE_PARENT_PAGE_TITLE": settings["source_hub"],
            "EVAL_PARENT_PAGE_TITLE": settings["evaluation_hub"],
            "PLAYWRIGHT_HEADLESS": "true",
            "PLAYWRIGHT_BROWSER": browser_options(settings, default="firefox")["browser"],
        }.items():
            os.environ[key] = value
        registry = importlib.import_module("src.config.config_schema").ConfigRegistry
        registry._instances.pop("notion", None)
        return native.NotionStateManager(
            source_notion_key=private["source_key"],
            eval_notion_key=private["evaluation_key"],
            source_parent_page_title=settings["source_hub"],
            eval_parent_page_title=settings["evaluation_hub"],
            **browser_options(settings, default="firefox"),
        )
    if service == "playwright":
        return native.PlaywrightStateManager(**browser_options(settings))
    site = settings["site"]
    expected = native.PlaywrightStateManager.CATEGORY_CONFIGS.get(category)
    if not expected or (site["image_alias"], site["container_alias"]) != (
        expected["image_name"],
        expected["container_name"],
    ):
        raise NativeConfigurationError("WebArena site differs from its native category")
    state = native.PlaywrightStateManager(
        docker_image_name=site["image_alias"],
        docker_container_name=site["container_alias"],
        host_port=site["port"],
        container_port=site["port"],
        readiness_timeout_seconds=settings.get("readiness_timeout_seconds", 600),
        **browser_options(settings),
    )
    state.CATEGORY_CONFIGS = deepcopy(state.CATEGORY_CONFIGS)
    state.CATEGORY_CONFIGS[category]["host_port"] = site["port"]

    def command(argv, *, check=False):
        result = effect("webarena.command", {"argv": argv})
        value = subprocess.CompletedProcess(
            argv, result["exit_code"], result["stdout"], result["stderr"]
        )
        if check:
            value.check_returncode()
        return value

    state._run_cmd = command
    return state

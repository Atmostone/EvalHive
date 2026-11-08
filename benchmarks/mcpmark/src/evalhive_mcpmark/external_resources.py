"""Declare external native worlds and one owned WebArena site without host ports."""

from __future__ import annotations

import math
from copy import deepcopy

from .external_state import validate_settings
from .native import NativeConfigurationError

NATIVE_SITE_CAPABILITIES = [
    "AUDIT_WRITE",
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "MKNOD",
    "NET_BIND_SERVICE",
    "NET_RAW",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
    "SYS_CHROOT",
]


def configure_assets(assets, service, settings, *, world_image, site_image=None, site_limits=None):
    from .installation import container, image_pin

    validate_settings(service, settings)
    image_pin(world_image)
    world, task = assets["mcpmark.world"], assets["mcpmark.task"]
    world["image"] = world_image
    if service in {"github", "notion"}:
        world["finalizer"] = {
            "argv": ["/opt/venv/bin/python", "-m", "evalhive_mcpmark.finalizer"],
            "timeout_seconds": 45,
            "replay_safe": True,
        }
    for member in (world, task):
        member["storage_slots"]["working"]["target"] = "/workspace"
    task["outputs"]["working"]["path"] = "/workspace"
    if service != "github":
        world["limits"]["memory_mb"] = world["minimum"]["memory_mb"] = 1024
        task["limits"]["memory_mb"] = task["minimum"]["memory_mb"] = 1536
    assets["mcpmark.network"] = {"kind": "network", "egress": service != "playwright_webarena"}
    names = ["working", "connection", "network", "task", "world"]
    if service == "playwright_webarena":
        image_pin(site_image)
        if not isinstance(site_limits, dict) or set(site_limits) != {
            "memory_mb",
            "cpu_cores",
            "disk_mb",
        }:
            raise NativeConfigurationError("WebArena requires explicit native site resource minima")
        if (
            any(
                type(site_limits[name]) is not int or site_limits[name] < 1
                for name in ("memory_mb", "disk_mb")
            )
            or isinstance(site_limits["cpu_cores"], bool)
            or not isinstance(site_limits["cpu_cores"], (int, float))
            or not math.isfinite(site_limits["cpu_cores"])
            or site_limits["cpu_cores"] <= 0
        ):
            raise NativeConfigurationError(
                "Native site resource minima must be finite and positive"
            )
        assets["mcpmark.site"] = container(
            site_image,
            "service",
            site_limits["memory_mb"],
            site_limits["cpu_cores"],
            site_limits["disk_mb"],
            capabilities=NATIVE_SITE_CAPABILITIES.copy(),
            mounts=[
                {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}
            ],
        )
        names.append("site")
    elif site_image is not None or site_limits is not None:
        raise NativeConfigurationError("Only WebArena accepts a native site image")
    return names


def environment_plan(bindings, service, agent):
    resources = [
        {
            "id": name,
            "kind": kind,
            "provider": "evalhive.rootless",
            "config": {"asset_id": bindings["assets"][name]},
        }
        for name, kind in (("working", "volume"), ("connection", "volume"), ("network", "network"))
    ]
    task = {
        "id": "task",
        "kind": "container",
        "provider": "evalhive.rootless",
        "config": {
            "asset_id": bindings["assets"]["task"],
            "network": "network",
            "storage": {"working": "working", "connection": "connection"},
        },
        "depends_on": ["working", "connection", "network"],
    }
    world = deepcopy(task)
    world["id"], world["config"]["asset_id"] = "world", bindings["assets"]["world"]
    if service in {"github", "notion"}:
        task["depends_on"].append("world")
        resources.extend([world, task])
        return _plan(resources, service, agent)
    resources.append(task)
    if service == "playwright_webarena":
        del world["config"]["network"]
        world["config"]["namespace"] = "task"
        world["depends_on"] = ["working", "connection", "task"]
        resources += [
            world,
            {
                "id": "site",
                "kind": "container",
                "provider": "evalhive.rootless",
                "config": {"asset_id": bindings["assets"]["site"], "namespace": "task"},
                "depends_on": ["task"],
            },
        ]
    else:
        resources.append(world)
    return _plan(resources, service, agent)


def _plan(resources, service, agent):
    return {
        "resources": resources,
        "agent": agent,
        "requirements": {"capabilities": ["host.resource_finalizers_v1"]}
        if service in {"github", "notion"}
        else {},
        "grading_mode": "environment",
        "outputs": [
            {
                "id": name,
                "resource_id": "world",
                "path": "/records/" + name,
                "visibility": "evaluator",
                "required": False,
                "stage": "evaluation" if name == "report" else "capture",
            }
            for name in ("transcript", "report")
        ]
        + [
            {
                "id": "working",
                "resource_id": "task",
                "path": "/workspace",
                "visibility": "user",
                "required": False,
                "stage": "capture",
            }
        ],
    }

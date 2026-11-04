"""Public execution conditions derived from the applied attempt specification."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def conditions(attempt):
    spec, manifest = attempt.run_spec, attempt.manifest
    model = {key: value for key, value in (spec.get("model") or {}).items() if key != "credential_ref"}
    resources = deepcopy(spec.get("environment", {}).get("resources", []))
    for resource in resources:
        if resource["id"] == "evalhive_knowledge":
            resource["config"]["url"] = "evalhive://current-attempt/knowledge/mcp"
    core = {
        "connector_id": attempt.connector_id, "connector_version": attempt.connector_version,
        "protocol_version": attempt.protocol_version,
        "harness_version": manifest.get("requirements", {}).get("harness_version"),
        "image_id": spec.get("metadata", {}).get("image_id"),
        "model": model, "config": spec["config"], "instructions": spec.get("instructions", []),
        "limits": spec.get("limits", {}),
        "execution_mode": spec.get("metadata", {}).get("execution_mode"),
    }
    full = {**core, "environment": {**spec.get("environment", {}), "resources": resources}}
    return full, core


def condition_fingerprints(attempt):
    full, core = conditions(attempt)
    return digest(full), digest(core)


def execution_context(attempt):
    spec = attempt.run_spec
    resources = spec.get("environment", {}).get("resources", [])
    tools = next((item["config"]["names"] for item in resources if item["kind"] == "tools"), None)
    memory = next((item["config"] for item in resources if item["kind"] == "memory"), {"mode": "off"})
    full, core = conditions(attempt)
    return {
        "template_id": spec.get("metadata", {}).get("template_id"),
        "template_name": spec.get("metadata", {}).get("template_name"),
        "model_api_name": (spec.get("model") or {}).get("name"),
        "soul_md": "\n\n".join(spec.get("instructions", [])),
        "tools": tools, "mcp_servers": [item["id"] for item in resources if item["kind"] == "mcp"],
        "resource_limits": spec.get("limits", {}),
        "memory_context": memory.get("content", "") if memory["mode"] != "off" else "",
        "flat_memory": {},
        "harness": {**core, "environment": full.get("environment"),
                    "task": deepcopy(spec["task"]),
                    "credential_roles": list(spec.get("credential_refs", {})),
                    "model_id": spec.get("metadata", {}).get("model_id"),
                    "profile_id": spec.get("metadata", {}).get("profile_id"),
                    "template_updated_at": spec.get("metadata", {}).get("template_updated_at"),
                    "template_id": spec.get("metadata", {}).get("template_id"),
                    "template_name": spec.get("metadata", {}).get("template_name"),
                    "prepared_at": attempt.prepared_at.isoformat() if attempt.prepared_at else None,
                    "requested_config": spec.get("metadata", {}).get("requested_config"),
                    "configuration_state": "prepared" if attempt.prepared_at else "unknown",
                    "manifest_sha256": digest(attempt.manifest)},
    }

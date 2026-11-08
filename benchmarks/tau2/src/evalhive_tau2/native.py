"""Pinned native catalogs inside an explicitly isolated data environment."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import stat
from pathlib import Path

VERSION = "1.0.1"
REVISION = "fc0055dc4e0a316c3f83133267fbd6faaa770992"
DOMAINS = ("airline", "retail", "telecom")
MAX_FILE_BYTES = 32 * 1024**2
MAX_SOURCE_BYTES = 128 * 1024**2
MAX_TASKS = 10000


class NativeConfigurationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def fingerprint(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def data_root(root):
    root = Path(root).absolute()
    if not root.is_dir() or root.resolve(strict=True) != root:
        raise NativeConfigurationError(
            "Native data root must be an installed directory without links"
        )
    return root


def source_files(domain):
    if domain not in DOMAINS:
        raise NativeConfigurationError("Native baseline domain is unsupported")
    names = ["tasks.json", "split_tasks.json"]
    names += (
        [
            "db.toml",
            "user_db.toml",
            "main_policy.md",
            "tech_support_manual.md",
            "tech_support_workflow.md",
        ]
        if domain == "telecom"
        else ["db.json", "policy.md"]
    )
    return ["tau2/domains/" + domain + "/" + name for name in names] + [
        "tau2/user_simulator/simulation_guidelines.md",
        "tau2/user_simulator/simulation_guidelines_tools.md",
    ]


def inventory(root, domain):
    root, total, result = data_root(root), 0, []
    for relative in sorted(source_files(domain)):
        path = root / relative
        if path.resolve(strict=True) != path:
            raise NativeConfigurationError("Native data cannot traverse installation links")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise NativeConfigurationError("Native data exceeds its regular-file budget")
            content = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        total += len(content)
        if (
            len(content) != before.st_size
            or total > MAX_SOURCE_BYTES
            or any(
                getattr(before, field) != getattr(after, field)
                for field in (
                    "st_ino",
                    "st_dev",
                    "st_mode",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            )
        ):
            raise NativeConfigurationError("Native data changed or exceeded its inventory budget")
        result.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return result


def load_domain(root, domain):
    root = data_root(root)
    if domain not in DOMAINS:
        raise NativeConfigurationError("Native baseline domain is unsupported")
    # Upstream imports load_dotenv at module scope. Require its supported opt-out
    # before importing any native module, including administrator catalog reads.
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1" or os.environ.get("TAU2_DATA_DIR") != str(
        root
    ):
        raise NativeConfigurationError(
            "Native imports require explicit data and disabled dotenv loading"
        )
    if importlib.metadata.version("tau2") != VERSION:
        raise NativeConfigurationError("Native package differs from the pinned version")
    module = importlib.import_module("tau2.domains." + domain + ".environment")
    utils = importlib.import_module("tau2.utils.utils")
    if Path(utils.DATA_DIR).absolute() != root:
        raise NativeConfigurationError("Native process is already bound to another data root")
    return module


def catalog(root, domain, split="base"):
    if not isinstance(split, str) or not split or len(split) > 200:
        raise NativeConfigurationError("A native task split must be selected explicitly")
    module = load_domain(root, domain)
    splits = module.get_tasks_split()
    if split not in splits:
        raise NativeConfigurationError("Native task split is unavailable")
    tasks = module.get_tasks(task_split_name=split)
    ids = [task.id for task in tasks]
    selected = splits[split]
    if (
        not tasks
        or len(tasks) > MAX_TASKS
        or len(set(ids)) != len(ids)
        or len(set(selected)) != len(selected)
        or set(selected) != set(ids)
    ):
        raise NativeConfigurationError("Native split contains duplicate or unavailable tasks")
    return tasks

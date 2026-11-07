"""Load pinned native managers only in an explicitly isolated process."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import sys
from pathlib import Path

REVISION = "cd45b7f57923b9b3985467f5139927575f83141c"
SERVICES = ("filesystem", "postgres", "github", "notion", "playwright", "playwright_webarena")
SUITES = ("standard", "easy")
MAX_FILE_BYTES = 32 * 1024**2
MAX_SOURCE_BYTES = 128 * 1024**2
MAX_ENTRIES = 10000
CODE_LOCK = json.loads(Path(__file__).with_name("native-code.lock.json").read_text())


class NativeConfigurationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def fingerprint(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def directory(root):
    root = Path(root).absolute()
    if not root.is_dir() or root.resolve(strict=True) != root:
        raise NativeConfigurationError("Native directories must not traverse links")
    return root


def file_record(root, path):
    if path.resolve(strict=True) != path:
        raise NativeConfigurationError("Native source cannot traverse links")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise NativeConfigurationError("Native source exceeds its regular-file budget")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) != before.st_size or any(
        getattr(before, field) != getattr(after, field)
        for field in ("st_ino", "st_dev", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    ):
        raise NativeConfigurationError("Native source changed during inspection")
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def variant(service, suite):
    if service not in SERVICES or suite not in SUITES:
        raise NativeConfigurationError("Native service or suite is unsupported")


def inventory(root, service, suite):
    root = directory(root)
    variant(service, suite)
    selected = root / "tasks" / service / suite
    if selected.resolve() != selected:
        raise NativeConfigurationError("Native dataset cannot traverse links")
    if not selected.is_dir():
        raise NativeConfigurationError("Native service suite is absent at the pinned revision")
    result, total = [], 0
    for path in sorted(selected.rglob("*")):
        if path.is_symlink():
            raise NativeConfigurationError("Native dataset cannot contain links")
        if path.is_dir():
            continue
        record = file_record(root, path)
        result.append(record)
        total += record["size_bytes"]
        if len(result) > MAX_ENTRIES or total > MAX_SOURCE_BYTES:
            raise NativeConfigurationError("Native dataset exceeds its inventory budget")
    return result


def native_code():
    value = os.environ.get("EVALHIVE_MCPMARK_NATIVE_ROOT")
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1" or not value:
        raise NativeConfigurationError(
            "Native imports require disabled dotenv and an explicit code root"
        )
    root = directory(value)
    if CODE_LOCK["revision"] != REVISION:
        raise NativeConfigurationError("Native code lock belongs to another revision")
    for expected in CODE_LOCK["files"]:
        if file_record(root, root / expected["path"]) != expected:
            raise NativeConfigurationError("Native manager source differs from the pinned revision")
    actual = {path.relative_to(root).as_posix() for path in (root / "src").rglob("*.py")}
    if actual != {record["path"] for record in CODE_LOCK["files"]}:
        raise NativeConfigurationError("Native code contains unregistered Python modules")
    for name, module in tuple(sys.modules.items()):
        if name != "src" and not name.startswith("src."):
            continue
        filename = getattr(module, "__file__", None)
        locations = (
            [Path(filename)]
            if filename
            else [Path(path) for path in getattr(module, "__path__", [])]
        )
        if any(not path.is_relative_to(root / "src") for path in locations):
            raise NativeConfigurationError("Native process already loaded a foreign src package")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def task_manager(root, service, suite):
    variant(service, suite)
    code = native_code()
    definitions = importlib.import_module("src.services").SERVICES
    target = definitions[service]["components"]["task_manager"]
    module_name, name = target.rsplit(".", 1)
    module = importlib.import_module(module_name)
    if Path(module.__file__).resolve() != code / (module_name.replace(".", "/") + ".py"):
        raise NativeConfigurationError("Native task manager loaded from another code root")
    return getattr(module, name)(tasks_root=directory(root) / "tasks", task_suite=suite)


def catalog(root, service, suite):
    root = directory(root)
    inventory(root, service, suite)
    manager = task_manager(root, service, suite)
    tasks = manager.discover_all_tasks()
    if not tasks or len(tasks) > MAX_ENTRIES:
        raise NativeConfigurationError("Native service suite has no bounded task catalog")
    seen = set()
    for task in tasks:
        identity = task.category_id, str(task.task_id)
        if identity in seen:
            raise NativeConfigurationError("Native task identities are duplicated")
        seen.add(identity)
        for path in (task.task_instruction_path, task.task_verification_path):
            if not path.is_relative_to(root / "tasks" / service / suite):
                raise NativeConfigurationError("Native task paths leave their selected suite")
            file_record(root, path)
    return manager, tasks

"""Build installed native MCP projects and record exact dependency inputs."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path("/opt/local_servers")
METADATA = Path("/opt/build-info")
METADATA.mkdir(parents=True, exist_ok=True)
MODE = os.environ.get("EVALHIVE_NATIVE_LOCK_MODE", "frozen")
if MODE not in {"frozen", "resolve"}:
    raise RuntimeError("Unsupported native dependency lock mode")
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("phase", choices=("resolve", "install", "build", "verify"))
parser.add_argument("--project")
options = parser.parse_args()


def run(argv, cwd):
    print("Native dependency build:", str(cwd), " ".join(argv[:3]), flush=True)
    subprocess.run(argv, cwd=cwd, check=True)


def save_changed_lock(path, before):
    if not path.is_file():
        raise RuntimeError("Frozen native dependency lock is unavailable: " + str(path))
    if path.read_bytes() != before:
        destination = METADATA / "generated-locks" / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())


projects, artifacts, native = {}, [], {}
for path in sorted(Path("/opt/native-mcp-configs").glob("*.yaml")):
    document = yaml.safe_load(path.read_text())
    params = document["params"]
    command = params["command"].replace("${local_servers_paths}", str(ROOT))
    args = [value.replace("${local_servers_paths}", str(ROOT)) for value in params.get("args", [])]
    if command == "uv":
        project = Path(args[args.index("--directory") + 1])
        kind = "python"
    elif command == "node":
        entrypoint = Path(args[0])
        project = entrypoint.parent
        while not (project / "package.json").is_file() and project.is_relative_to(ROOT):
            project = project.parent
        if not project.is_relative_to(ROOT):
            raise RuntimeError("Native Node MCP package is unavailable")
        artifacts.append(entrypoint)
        kind = "node"
    elif command.startswith(str(ROOT) + "/") and "/.venv/bin/python" in command:
        entrypoint = Path(command)
        project = entrypoint.parents[2]
        artifacts.append(entrypoint)
        kind = "python"
    else:
        raise RuntimeError("Unsupported native MCP executable")
    projects[project] = kind
    native[document["name"]] = {"kind": kind, "project": str(project)}

locks, installed = {}, set()
for project, kind in sorted(projects.items()):
    if options.project and project.name != options.project:
        continue
    if kind == "python":
        lock = project / "uv.lock"
        before = lock.read_bytes() if lock.is_file() else None
        if options.phase == "resolve" and MODE == "resolve":
            run(["uv", "lock", "--python", os.environ["UV_PYTHON"]], project)
        if options.phase == "resolve":
            save_changed_lock(lock, before)
        if options.phase == "install":
            run(
                ["uv", "sync", "--locked", "--no-dev", "--python", os.environ["UV_PYTHON"]], project
            )
    else:
        install_root = project
        while not (
            install_root / "package-lock.json"
        ).is_file() and install_root.parent.is_relative_to(ROOT):
            install_root = install_root.parent
        if not (install_root / "package-lock.json").is_file():
            install_root = project
        lock = install_root / "package-lock.json"
        before = lock.read_bytes() if lock.is_file() else None
        if install_root not in installed and options.phase in {"resolve", "install"}:
            if options.phase == "resolve" and MODE == "resolve":
                run(
                    [
                        "npm",
                        "install",
                        "--package-lock-only",
                        "--ignore-scripts",
                        "--no-audit",
                        "--no-fund",
                    ],
                    install_root,
                )
            if options.phase == "resolve":
                save_changed_lock(lock, before)
            else:
                run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], install_root)
            installed.add(install_root)
        manifest = json.loads((project / "package.json").read_text())
        if options.phase == "build" and "build" in manifest.get("scripts", {}):
            run(["npm", "run", "build"], project)
    locks[str(lock)] = "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
for artifact in artifacts:
    if options.phase == "verify" and not artifact.is_file():
        raise RuntimeError("Native MCP build did not produce its entrypoint: " + str(artifact))
if options.phase in {"resolve", "verify"}:
    (METADATA / "mcp.json").write_text(
        json.dumps({"servers": native, "locks": locks, "lock_mode": MODE}, indent=2) + "\n"
    )

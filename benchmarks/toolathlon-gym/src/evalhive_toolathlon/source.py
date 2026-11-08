"""Read pinned native task metadata from a verified archive, without host extraction."""

from __future__ import annotations

import hashlib
import json
import tarfile
from copy import deepcopy
from pathlib import Path, PurePosixPath

from .commands import NativeConfigurationError, task_path
from .mcp import resource

REVISION = "ed735ba0af71d82688a55f952a6ea11479ae7206"
TREE = "e90ee7afdbed88b4cf20d3d23c9e85bb092e8b2b"
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_METADATA_BYTES = 1024**2


def file_digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise NativeConfigurationError("Native source must be a bounded regular installed archive")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise NativeConfigurationError("Native source archive exceeds its byte limit")
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class SourceArchive:
    def __init__(self, path, expected_digest):
        self.path = Path(path)
        if file_digest(path) != expected_digest:
            raise NativeConfigurationError("Native source archive differs from its frozen digest")
        self.digest = expected_digest
        self.catalog = None
        try:
            self.members = {}
            with tarfile.open(self.path, "r:") as archive:
                for member in archive:
                    if len(self.members) >= 65536:
                        raise NativeConfigurationError(
                            "Native source archive contains too many entries"
                        )
                    native = PurePosixPath(member.name)
                    if (
                        native.is_absolute()
                        or ".." in native.parts
                        or "\\" in member.name
                        or not member.isfile()
                        and not member.isdir()
                        or member.name in self.members
                    ):
                        raise NativeConfigurationError(
                            "Native source archive contains unsupported entries"
                        )
                    self.members[member.name] = member
        except (tarfile.TarError, OSError):
            raise NativeConfigurationError("Native source archive cannot be indexed") from None

    def read(self, name):
        member = self.members.get(name)
        if member is None or not member.isfile() or member.size > MAX_METADATA_BYTES:
            raise NativeConfigurationError("Native metadata file is unavailable or oversized")
        try:
            with tarfile.open(self.path, "r:") as archive:
                with archive.extractfile(member) as stream:
                    data = stream.read(MAX_METADATA_BYTES + 1)
            if len(data) != member.size:
                raise NativeConfigurationError("Native metadata file is incomplete")
            return data
        except (tarfile.TarError, OSError):
            raise NativeConfigurationError("Native metadata file cannot be read") from None

    def case_ids(self):
        prefix = "tasks/finalpool/"
        return sorted(
            name[len(prefix) :].split("/")[0]
            for name in self.members
            if name.startswith(prefix)
            and name.endswith("/task_config.json")
            and len(PurePosixPath(name).parts) == 4
        )

    def case(self, identity, *, suite="toolathlon-gym"):
        root = task_path(identity).relative_to("/gym").as_posix()
        try:
            config = json.loads(self.read(root + "/task_config.json"))
            description = self.read(root + "/docs/task.md").decode("utf-8")
        except (ValueError, UnicodeError):
            raise NativeConfigurationError("Native task metadata is invalid") from None
        if not isinstance(config, dict):
            raise NativeConfigurationError("Native task configuration must be an object")
        needed = config.get("needed_mcp_servers")
        if (
            not isinstance(needed, list)
            or any(not isinstance(name, str) for name in needed)
            or len(set(needed)) != len(needed)
        ):
            raise NativeConfigurationError("Native task MCP requirements are invalid")
        base = "${TOOLATHLON_GYM_PATH}/" + root
        for phase in ("preprocess", "evaluation"):
            member = self.members.get(root + "/" + phase + "/main.py")
            if member is None or not member.isfile():
                raise NativeConfigurationError("Native task phase script is missing")
        groundtruth = self.members.get(root + "/groundtruth_workspace")
        has_gold = groundtruth is not None and groundtruth.isdir()
        external = {
            "preprocess_command": "python "
            + base
            + "/preprocess/main.py --agent_workspace ${AGENT_WORKSPACE} --launch_time ${LAUNCH_TIME}",
            "eval_command": "python "
            + base
            + "/evaluation/main.py --agent_workspace ${AGENT_WORKSPACE} "
            + ("--groundtruth_workspace ${GROUNDTRUTH_WORKSPACE} " if has_gold else "")
            + "--launch_time ${LAUNCH_TIME} --res_log_file ${RES_LOG_FILE}",
        }
        if has_gold:
            external["groundtruth_path"] = root + "/groundtruth_workspace"
        return {
            "id": identity,
            "suite": suite,
            "input": {
                "title": identity.replace("-", " ").replace("_", " ").title(),
                "description": description,
            },
            "gold": {
                "external_eval": external,
                "capability_spec": {"required_tools": needed, "match": "all"},
            },
            "environment": {"required_services": ["toolathlon_pg"], "mcp_servers": needed},
            "meta": {
                "source": "toolathlon_gym",
                "source_revision": REVISION,
                "source_tree": TREE,
                "native_archive_digest": self.digest,
                "task_path": root,
                "native_config": config,
            },
        }

    def mcp_resources(self, needed):
        import yaml
        from evalhive_cases.format import UniqueLoader

        if (
            not isinstance(needed, list)
            or any(not isinstance(name, str) for name in needed)
            or len(set(needed)) != len(needed)
        ):
            raise NativeConfigurationError("Native source MCP requirements are invalid")
        if self.catalog is not None:
            if any(name not in self.catalog for name in needed):
                raise NativeConfigurationError("Native source MCP requirements are unavailable")
            return [deepcopy(self.catalog[name]) for name in needed]
        catalog = {}
        for name in sorted(self.members):
            if not name.startswith("configs/mcp_servers/") or not name.endswith(".yaml"):
                continue
            try:
                document = yaml.load(self.read(name), Loader=UniqueLoader)
                mapped = resource(document)
                identity = document["name"]
                if identity in catalog:
                    raise NativeConfigurationError("Native source contains ambiguous MCP names")
                catalog[identity] = mapped
            except (ValueError, TypeError, yaml.YAMLError, RecursionError):
                raise NativeConfigurationError("Native source MCP declaration is invalid") from None
        if any(name not in catalog for name in needed):
            raise NativeConfigurationError("Native source MCP requirements are unavailable")
        self.catalog = catalog
        return [deepcopy(catalog[name]) for name in needed]

    def agent_instructions(self, identity, launch_time):
        """Preserve public native system instructions with Task-local bindings."""
        name = task_path(identity).relative_to("/gym").as_posix() + "/docs/agent_system_prompt.md"
        if name not in self.members:
            return []
        try:
            text = self.read(name).decode("utf-8")
        except UnicodeError:
            raise NativeConfigurationError("Native agent instructions are invalid") from None
        bindings = {
            "workspace_dir": "/workspace",
            "current_working_dir": "/workspace",
            "workspace_dir_rela": ".",
            "time": launch_time,
        }
        for key, value in bindings.items():
            text = text.replace("!!<<<<||||" + key + "||||>>>>!!", value)
        if "!!<<<<" in text or "\0" in text:
            raise NativeConfigurationError("Native agent instructions have unsupported bindings")
        return [text] if text.strip() else []

"""Provider-owned native assets; emitted as data without importing backend code."""

from __future__ import annotations

from copy import deepcopy

from .dataset import CONNECTOR_ID, CONNECTOR_VERSION

PG_ENV = {
    "PGHOST": "toolathlon_pg",
    "PGPORT": "5432",
    "PGDATABASE": "toolathlon_gym",
    "PGUSER": "eigent",
    "PGPASSWORD": "camel",
}


def assets(worker_image, environment_image, postgres_image, *, egress=True):
    """Images must be resolved by the installer to immutable local IDs/digests."""

    def container(image, role, memory, cpu, disk, *, pids=128, **settings):
        return {
            "kind": "container",
            "role": role,
            "image": image,
            "limits": {"memory_mb": memory, "cpu_cores": cpu, "pids": pids},
            "minimum": {"memory_mb": memory, "cpu_cores": cpu, "disk_mb": disk},
            **settings,
        }

    workspace = {"workspace": {"target": "/workspace", "read_only": False}}
    native = {
        "worker": container(
            worker_image,
            "worker",
            256,
            0.1,
            64,
            mounts=[
                {"file_id": "cases", "target": "/dataset/cases", "read_only": True},
                {
                    "file_id": "native-source",
                    "target": "/dataset/native-source.tar",
                    "read_only": True,
                },
            ],
        ),
        "network": {"kind": "network", "egress": egress},
        "workspace": {"kind": "volume", "disk_mb": 256},
        "database-files": {"kind": "volume", "disk_mb": 512},
        "initialize-files": container(
            environment_image,
            "service",
            64,
            0.1,
            64,
            command=["/opt/venv/bin/python", "-c", "import os;os.chmod('/data',0o777)"],
            storage_slots={"database": {"target": "/data", "read_only": False}},
        ),
        "database": container(
            postgres_image,
            "service",
            256,
            0.1,
            0,
            user="999:999",
            environment={
                "POSTGRES_USER": "eigent",
                "POSTGRES_PASSWORD": "camel",
                "POSTGRES_DB": "toolathlon_gym",
                "PGDATA": "/data/pgdata",
            },
            storage_slots={"database": {"target": "/data", "read_only": False}},
            command=["postgres", "-c", "shared_buffers=32MB", "-c", "max_connections=40"],
        ),
        "world": container(
            environment_image,
            "service",
            512,
            0.1,
            512,
            pids=256,
            command=["/opt/venv/bin/python", "-c", "import time;time.sleep(86400)"],
            environment=deepcopy(PG_ENV),
            storage_slots=deepcopy(workspace),
            mounts=[
                {
                    "file_id": "native-source",
                    "target": "/native/native-source.tar",
                    "read_only": True,
                }
            ],
            outputs={
                "workspace": {
                    "path": "/workspace",
                    "visibility": "user",
                    "required": True,
                    "max_archive_bytes": 64 * 1024**2,
                },
                "checkpoint": {
                    "path": "/checkpoint",
                    "visibility": "private",
                    "required": False,
                    "max_archive_bytes": 256 * 1024**2,
                },
                "native-report": {
                    "path": "/reports",
                    "visibility": "evaluator",
                    "required": False,
                    "stage": "evaluation",
                    "max_archive_bytes": 16 * 1024**2,
                },
            },
        ),
        "task": container(
            environment_image,
            "environment",
            1024,
            1,
            128,
            pids=256,
            command=["/opt/venv/bin/python", "-c", "import time;time.sleep(86400)"],
            environment={
                **PG_ENV,
                "PG_HOST": "toolathlon_pg",
                "PG_PORT": "5432",
                "PG_USER": "eigent",
                "PG_PASSWORD": "camel",
                "PG_DATABASE": "toolathlon_gym",
            },
            workdir="/workspace",
            storage_slots=deepcopy(workspace),
            mounts=[
                {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}
            ],
        ),
    }
    return {
        "format_version": "1.0",
        "connector_id": CONNECTOR_ID,
        "connector_version": CONNECTOR_VERSION,
        "provider_id": "evalhive.rootless",
        "worker_asset": "toolathlon-gym.worker",
        "assets": {"toolathlon-gym." + name: value for name, value in native.items()},
    }

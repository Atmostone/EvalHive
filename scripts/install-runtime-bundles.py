#!/usr/bin/env python3
"""Install already-built, catalog-declared runtime bundles on the execution host."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import docker  # noqa: E402
from app.runtime.bundles import install_bundle  # noqa: E402
from app.runtime.managed_docker import RootlessDockerRuntime  # noqa: E402
from app.runtime.managed_spec import PathMapping, RuntimePolicy  # noqa: E402
from evalhive_harness import validate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    options = parser.parse_args()
    data = str(options.data_root.resolve(strict=True))
    root = str(options.bundle_root.absolute())
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock")
    try:
        runtime = RootlessDockerRuntime(client, RuntimePolicy(PathMapping(data, data)))
        for path in options.manifest:
            declaration = validate("manifest", json.loads(path.read_text()))
            result = install_bundle(runtime, declaration, PathMapping(root, root))
            print(
                json.dumps(
                    {
                        "connector_id": result.connector_id,
                        "connector_version": result.connector_version,
                        "image_id": result.image_id,
                        "fingerprint": result.fingerprint,
                    }
                )
            )
    finally:
        client.close()


if __name__ == "__main__":
    main()

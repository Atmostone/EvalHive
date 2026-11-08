"""Build a new immutable case snapshot and coordinator registration offline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark.installation import dataset_key, installation_digest
from evalhive_benchmark.protocol import MAX_LINE_BYTES

from .dataset import build_receipt
from .format import SUFFIXES, CaseFormatError, load_suite


def write_json(path, value):
    encoded = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o444)


def install(source, output, *, suite, revision, worker_image, dataset_id=None):
    """Output must be new. A failed build never publishes a partial snapshot."""
    source, output = Path(source), Path(output)
    if output.exists() or output.is_symlink():
        raise CaseFormatError("Installation output already exists")
    original = build_receipt(source, suite, revision, worker_image, dataset_id=dataset_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evalhive-cases-", dir=output.parent))
    try:
        native = staging / "native"
        directory = native / suite
        directory.mkdir(parents=True)
        for path in sorted((source / suite).iterdir()):
            if path.suffix.lower() in SUFFIXES:
                if path.is_symlink() or not path.is_file():
                    raise CaseFormatError("Installed sources must remain regular files")
                # Bound reads even if a source file grows while it is copied.
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise CaseFormatError("Installed sources must remain regular files")
                    data = stream.read(MAX_LINE_BYTES + 1)
                if len(data) > MAX_LINE_BYTES:
                    raise CaseFormatError("Native source exceeds its byte limit")
                destination = directory / path.name
                with destination.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                destination.chmod(0o444)
        copied = build_receipt(native, suite, revision, worker_image, dataset_id=dataset_id)
        if copied != original:
            raise CaseFormatError("Native source changed while building the installation")
        load_suite(native, suite)
        write_json(native / "installation.json", copied)
        registration = staging / "registration" / "evalhive-cases"
        (registration / "datasets").mkdir(parents=True)
        manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())
        manifest["image"] = worker_image
        write_json(registration / "manifest.json", manifest)
        write_json(
            registration / "datasets" / (dataset_key(copied["dataset_id"], revision) + ".json"),
            copied,
        )
        # Both parts are published together, with an installation digest for deployment logs.
        write_json(
            staging / "receipt.json",
            {
                "dataset_id": copied["dataset_id"],
                "revision": revision,
                "installation_digest": installation_digest(copied),
                "source_digest": copied["source_digest"],
            },
        )
        os.rename(staging, output)
        return deepcopy(copied)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Administrator-provided case corpus root")
    parser.add_argument("--output", required=True, help="New snapshot directory")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--worker-image", required=True, help="Immutable image digest")
    parser.add_argument("--dataset-id")
    args = parser.parse_args()
    install(
        args.source,
        args.output,
        suite=args.suite,
        revision=args.revision,
        worker_image=args.worker_image,
        dataset_id=args.dataset_id,
    )


if __name__ == "__main__":
    main()

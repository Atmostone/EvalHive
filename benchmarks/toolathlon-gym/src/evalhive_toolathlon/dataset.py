"""Export a pinned public source revision and install native case metadata offline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from evalhive_cases.dataset import InstalledCases, build_receipt
from evalhive_cases.format import parse_case
from evalhive_cases.install import write_json

from .commands import NativeConfigurationError
from .source import REVISION, TREE, SourceArchive, file_digest

CONNECTOR_ID, CONNECTOR_VERSION = "toolathlon-gym", "0.1.0"


def export_source(repository, destination):
    repository, destination = Path(repository), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise NativeConfigurationError("Native source destination already exists")
    tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", REVISION + "^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if tree != TREE:
        raise NativeConfigurationError("Native source revision differs from the pinned tree")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            process = subprocess.Popen(
                ["git", "-C", str(repository), "archive", "--format=tar", REVISION],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                total = 0
                while data := process.stdout.read(1024**2):
                    total += len(data)
                    if total > 2 * 1024**3:
                        raise NativeConfigurationError(
                            "Native source export exceeds its byte limit"
                        )
                    stream.write(data)
                if process.wait(timeout=30) != 0:
                    raise NativeConfigurationError("Pinned native source export failed")
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
        destination.chmod(0o444)
        return file_digest(destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def install(repository, output, *, revision, worker_image, suite="toolathlon-gym", dataset_id=None):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise NativeConfigurationError("Native dataset output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".toolathlon-gym-", dir=output.parent))
    try:
        archive_path = staging / "native-source.tar"
        digest = export_source(repository, archive_path)
        archive = SourceArchive(archive_path, digest)
        directory = staging / "cases" / suite
        # Suite/path validity is checked with the portable reader before publication.
        if not suite or "/" in suite or "\\" in suite or suite in {".", ".."}:
            raise NativeConfigurationError("Native suite must be an installed directory name")
        directory.mkdir(parents=True)
        for identity in archive.case_ids():
            document = archive.case(identity, suite=suite)
            parse_case(json.dumps(document).encode(), suffix=".json")
            write_json(directory / (identity + ".json"), document)
        receipt = build_receipt(
            staging / "cases",
            suite,
            revision,
            worker_image,
            dataset_id=dataset_id,
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
        )
        receipt["provenance"].update(
            native_source_revision=REVISION, native_source_tree=TREE, native_archive_digest=digest
        )
        write_json(staging / "cases/installation.json", receipt)
        write_json(
            staging / "source.json", {"revision": REVISION, "tree": TREE, "archive_digest": digest}
        )
        os.rename(staging, output)
        return receipt
    finally:
        if staging.exists():
            shutil.rmtree(staging)


class InstalledToolathlon(InstalledCases):
    def __init__(self, root):
        self.native_root = Path(root)
        super().__init__(
            self.native_root / "cases",
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
        )
        provenance = self.receipt.get("provenance", {})
        if (provenance.get("native_source_revision"), provenance.get("native_source_tree")) != (
            REVISION,
            TREE,
        ):
            raise NativeConfigurationError("Installed native source pin is unavailable")
        self.archive = SourceArchive(
            self.native_root / "native-source.tar", provenance.get("native_archive_digest")
        )

    def resolve(self, descriptor):
        case = super().resolve(descriptor)
        if case.native_data()["meta"].get("native_archive_digest") != self.archive.digest:
            raise NativeConfigurationError("Case belongs to another native source archive")
        return case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--revision",
        required=True,
        help="New dataset revision, independent of the pinned source commit",
    )
    parser.add_argument("--worker-image", required=True, help="Installed immutable worker image")
    parser.add_argument("--suite", default="toolathlon-gym")
    parser.add_argument("--dataset-id")
    args = parser.parse_args()
    install(
        args.repository,
        args.output,
        revision=args.revision,
        worker_image=args.worker_image,
        suite=args.suite,
        dataset_id=args.dataset_id,
    )


if __name__ == "__main__":
    main()

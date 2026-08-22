"""CLI for the frozen reproduction bundle (SPA-90).

Export one experiment as a self-contained archive, and verify that archive
offline — no database, no object store, no provider calls. Run inside the api
container::

    docker compose exec api python -m app.cli.bundle export --experiment-id <uuid>
    docker compose exec api python -m app.cli.bundle export --experiment-id <uuid> --with-traces
    docker compose exec api python -m app.cli.bundle verify --bundle /tmp/bundle-<id>.tar.gz
    docker compose exec api python -m app.cli.bundle show   --bundle /tmp/bundle-<id>.tar.gz

``export`` freezes the rows, the profiles, the annotations and the MinIO blobs,
then computes the report twice — once through the platform's own ``compute_report``
and once through the offline path a reader will use — and **refuses to write a
bundle whose numbers do not already reproduce**. A bundle that exists is therefore
a bundle that verified at birth.

``verify`` rebuilds the report from the archive alone and compares it against the
frozen expectation on two levels: the named headline metrics (a mismatch is a
failure, exit 1) and the whole canonical report (a mismatch is a warning with a
key-path diff, exit 0). See :mod:`app.quality.bundle` for why those are different
questions.

``show`` prints the manifest, for a reader deciding whether to bother.

This module is argparse plumbing only; everything it does lives in
:mod:`app.quality.bundle`, so the HTTP endpoint and the tests exercise the same
code rather than a second copy of it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

from app.database import async_session
from app.models.experiment import Experiment
from app.quality.bundle import (
    MANIFEST_NAME,
    BundleMismatch,
    build_bundle,
    read_tar,
    verify_bundle,
    write_tar,
)
from app.quality.experiment_report import SELECTION_LATEST_VALID, SELECTION_POLICIES


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def _export(args: argparse.Namespace) -> int:
    exp_id = uuid.UUID(args.experiment_id)
    async with async_session() as db:
        exp = await db.get(Experiment, exp_id)
        if exp is None:
            _print({"error": "experiment not found"})
            return 2
        try:
            files, manifest = await build_bundle(
                db,
                exp,
                selection=args.selection,
                method=args.method,
                with_traces=args.with_traces,
            )
        except BundleMismatch as e:
            _print({"error": str(e), "diff": e.diff})
            return 1

    out = args.out or f"/tmp/bundle-{exp_id}.tar.gz"
    with open(out, "wb") as fh:
        fh.write(write_tar(files))
    _print({
        "wrote": out,
        "bytes": os.path.getsize(out),
        "counts": manifest["counts"],
        "coverage": manifest["coverage"],
        "verified_at_export": True,
    })
    return 0


async def _verify(args: argparse.Namespace) -> int:
    with open(args.bundle, "rb") as fh:
        result = verify_bundle(read_tar(fh.read()))
    _print(result)
    if not result["reproduced"]:
        print("FAILED: a headline metric did not reproduce", file=sys.stderr)
        return 1
    if not result["full_report_matches"]:
        print(
            "WARNING: the headline reproduced, but the full report moved — most "
            "likely a report schema change rather than a number. See "
            "full_report_diff.",
            file=sys.stderr,
        )
    return 0


async def _show(args: argparse.Namespace) -> int:
    with open(args.bundle, "rb") as fh:
        _print(json.loads(read_tar(fh.read())[MANIFEST_NAME]))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="bundle", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="freeze an experiment into a bundle")
    e.add_argument("--experiment-id", required=True)
    e.add_argument("--out", default=None)
    e.add_argument("--with-traces", action="store_true",
                   help="include agent log archives (the bulk of the object store)")
    e.add_argument("--selection", default=SELECTION_LATEST_VALID,
                   choices=list(SELECTION_POLICIES))
    e.add_argument("--method", default="bt", choices=["bt", "elo"])
    e.set_defaults(fn=_export)

    v = sub.add_parser("verify", help="recompute from a bundle and compare")
    v.add_argument("--bundle", required=True)
    v.set_defaults(fn=_verify)

    s = sub.add_parser("show", help="print a bundle's manifest")
    s.add_argument("--bundle", required=True)
    s.set_defaults(fn=_show)

    args = parser.parse_args()
    raise SystemExit(asyncio.run(args.fn(args)))


if __name__ == "__main__":
    main()

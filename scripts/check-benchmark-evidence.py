#!/usr/bin/env python3
"""Verify native evidence through the configured backend without printing locators.

Run inside a stopped-for-writes stand's backend, including an isolated restored
stand. This supplements the legacy S3-path checks in backup.sh/restore.sh: native
artifact locators are authenticated private records, not SQL path columns.
"""

import argparse
import asyncio
import json
import uuid


async def check(workspace=None, *, minimum=0):
    from app import database
    from app.benchmarks.evidence import reference
    from app.benchmarks.private_store import PrivateStore
    from app.benchmarks.product_runtime import evidence_store
    from app.benchmarks.trials import TrialStore
    from app.config import get_settings
    from app.models.benchmark_artifact import BenchmarkArtifact
    from sqlalchemy import inspect, select
    from sqlalchemy.ext.asyncio import AsyncSession

    report = {"status": "empty", "checked": 0, "verified_bytes": 0, "failed": 0, "errors": []}
    try:
        async with database.engine.connect() as connection:
            installed = await connection.run_sync(
                lambda sync: inspect(sync).has_table("benchmark_artifacts")
            )
        if not installed:
            report["status"] = "not_installed"
        else:
            store = TrialStore(database.engine, PrivateStore(get_settings().jwt_secret))
            evidence = evidence_store(store)
            cursor = None
            while True:
                query = select(BenchmarkArtifact).order_by(BenchmarkArtifact.id).limit(64)
                if workspace is not None:
                    query = query.where(BenchmarkArtifact.workspace_id == workspace)
                if cursor is not None:
                    query = query.where(BenchmarkArtifact.id > cursor)
                async with AsyncSession(database.engine) as db:
                    rows = list(await db.scalars(query))
                    if not rows:
                        break
                    for row in rows:
                        report["checked"] += 1
                        try:
                            async with evidence.open_verified(
                                row.workspace_id,
                                row.trial_id,
                                reference(row),
                                visibilities={"agent", "user", "evaluator", "private"},
                            ):
                                pass
                            report["verified_bytes"] += row.size_bytes
                        except Exception as error:
                            report["failed"] += 1
                            if len(report["errors"]) < 20:
                                # Never print native payloads, locators, credentials or
                                # exception messages from storage/cryptography libraries.
                                report["errors"].append(
                                    {"artifact_id": str(row.id), "error_type": type(error).__name__}
                                )
                    cursor = rows[-1].id
            if report["checked"]:
                report["status"] = "failed" if report["failed"] else "passed"
        if report["checked"] < minimum:
            report["status"] = "failed"
            report["minimum_artifacts"] = minimum
        return report
    finally:
        await database.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=uuid.UUID)
    parser.add_argument("--expect-min-artifacts", type=int, default=0)
    args = parser.parse_args()
    if args.expect_min_artifacts < 0:
        parser.error("The expected artifact count cannot be negative")
    try:
        report = asyncio.run(check(args.workspace, minimum=args.expect_min_artifacts))
    except Exception as error:
        report = {"status": "unavailable", "error_type": type(error).__name__}
    print(json.dumps(report, sort_keys=True))
    return 1 if report["status"] in {"failed", "unavailable"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Product API and durable queue acceptance with installed workers and real harnesses."""

import asyncio
import io
import json
import os
import tarfile
import traceback
import uuid
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import docker
import pytest
from app.api.benchmark_trials import get_trial_store
from app.benchmarks.dispatch import run_execution_job
from app.benchmarks.product_runtime import ProductRuntime, evidence_store
from app.config import get_settings
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkResourceLease
from app.models.experiment import Experiment, ExperimentRun
from app.models.harness_profile import HarnessProfile
from app.models.provider import LLMModel, Provider
from app.models.task import Task
from app.quality import experiments as matrix
from app.runtime.managed_spec import Quota
from evalhive_benchmark.installation import dataset_key
from sqlalchemy import select

from tests.fixtures.native_model_endpoint import NativeModelEndpoint

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_PRODUCT") != "1",
    reason="Requires explicit isolated rootless product acceptance",
)


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


@pytest.mark.parametrize("connector_id,mode", [("pi", "work"), ("opencode", "matrix")])
async def test_installed_native_package_runs_through_product_api_and_queue(
    auth_client, db_session, monkeypatch, connector_id, mode
):
    # Native imports belong to this engineering test, never to product modules.
    from evalhive_cases.dataset import build_receipt
    from evalhive_cases.worker import MANIFEST

    base = Path(os.environ["EVALHIVE_PRODUCT_FIXTURE_DIR"])
    host_base = Path(os.environ["EVALHIVE_PRODUCT_FIXTURE_HOST"])
    registry = base / "packages" / connector_id
    host_registry = host_base / "packages" / connector_id
    package = registry / "cases"
    source = package / "dataset"
    write(
        source / "fixture/case.json",
        {
            "id": "native-product-engineering",
            "suite": "fixture",
            "category": "engineering",
            "input": {
                "title": "Product native case",
                "description": "Run the native shell check and finish.",
            },
            "gold": {"reference_answer": "Completed", "rubric": {"private": "never-send-to-agent"}},
        },
    )
    client = docker.from_env()
    try:
        worker_image = client.images.get("evalhive-benchmark-cases:0.1.0").id
        task_image = client.images.get("evalhive-environment-workspace:0.1.0").id
        from app.harnesses.catalog import get_manifest

        harness_registry = base / "harnesses" / connector_id
        for name in ("pi", "opencode"):
            harness_manifest = deepcopy(get_manifest(name))
            harness_manifest["requirements"]["runtime_bundle"]["image"] = client.images.get(
                os.getenv(
                    f"EVALHIVE_{name.upper()}_BUNDLE_IMAGE", f"evalhive-{name}-bundle:development"
                )
            ).id
            write(harness_registry / name / "manifest.json", harness_manifest)
    finally:
        client.close()
    receipt = build_receipt(source, "fixture", "product-engineering-1", worker_image)
    declaration = {**deepcopy(MANIFEST), "image": worker_image}
    write(package / "manifest.json", declaration)
    write(source / "installation.json", receipt)
    write(
        package / "datasets" / (dataset_key(receipt["dataset_id"], receipt["revision"]) + ".json"),
        receipt,
    )
    write(
        package / "files.json",
        {
            "format_version": "1.0",
            "files": {
                "dataset": {
                    "kind": "relative",
                    "path": "dataset",
                    "source_digest": receipt["source_digest"],
                },
                "supervisor": {"kind": "runtime_binding", "name": "harness.runtime"},
            },
        },
    )
    write(
        package / "resources.json",
        {
            "format_version": "1.0",
            "connector_id": declaration["id"],
            "connector_version": declaration["version"],
            "provider_id": "evalhive.rootless",
            "worker_asset": "cases.worker",
            "assets": {
                "cases.worker": {
                    "kind": "container",
                    "role": "worker",
                    "image": worker_image,
                    "limits": asdict(Quota(256, 0.25, 128)),
                    "minimum": {"memory_mb": 256, "cpu_cores": 0.25, "disk_mb": 16},
                    "mounts": [{"file_id": "dataset", "target": "/dataset", "read_only": True}],
                },
                "evalhive-cases.task": {
                    "kind": "container",
                    "role": "environment",
                    "image": task_image,
                    "limits": asdict(Quota(1024, 1, 256)),
                    "minimum": {"memory_mb": 256, "cpu_cores": 1, "disk_mb": 16},
                    "command": [
                        "python",
                        "-c",
                        "from pathlib import Path;import time;Path('/workspace/result.txt').write_text('Prepared through the Benchmark SDK\\n');time.sleep(600)",
                    ],
                    "mounts": [
                        {"file_id": "supervisor", "target": "/evalhive-runtime", "read_only": True}
                    ],
                    "outputs": {
                        "workspace": {
                            "path": "/workspace",
                            "visibility": "user",
                            "required": True,
                            "max_archive_bytes": 2 * 1024**2,
                        }
                    },
                },
            },
        },
    )
    settings = get_settings()
    for name, value in {
        "benchmark_runtime_enabled": True,
        "harness_manifest_dir": str(harness_registry),
        "benchmark_manifest_dir": str(registry),
        "host_benchmark_manifest_dir": str(host_registry),
        "data_dir": str(base / "data"),
        "host_data_dir": str(host_base / "data"),
        "benchmark_evidence_bucket": "product-engineering-evidence",
    }.items():
        monkeypatch.setattr(settings, name, value)
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    credential = uuid.uuid4().hex
    with NativeModelEndpoint(credential) as endpoint:
        provider = Provider(
            workspace_id=workspace_id,
            name="Local product fixture",
            endpoint=endpoint.url,
            api_key=credential,
        )
        db_session.add(provider)
        await db_session.flush()
        model = LLMModel(
            provider_id=provider.id, display_name="Local fixture", api_name="fake-model"
        )
        db_session.add(model)
        await db_session.flush()
        if connector_id == "pi":
            harness = await db_session.scalar(
                select(HarnessProfile).where(HarnessProfile.workspace_id == workspace_id)
            )
            harness.model_id = model.id
        else:
            from app.harnesses.catalog import get_manifest

            manifest = get_manifest(connector_id)
            harness = HarnessProfile(
                workspace_id=workspace_id,
                name="Product OpenCode",
                connector_id=connector_id,
                connector_version=manifest["version"],
                config={"apiTimeoutSeconds": 120},
                secret_refs={},
                model_id=model.id,
            )
            db_session.add(harness)
        await db_session.commit()
        profile_response = await auth_client.post(
            "/api/benchmark-profiles",
            json={
                "name": "Product native fixture",
                "connector_id": declaration["id"],
                "connector_version": declaration["version"],
                "config": {
                    "evaluation_mode": "reference_exact",
                    "resources": [{"id": "tools", "kind": "tools", "config": {"names": ["bash"]}}],
                },
            },
        )
        assert profile_response.status_code == 201, profile_response.text
        profile_id = profile_response.json()["id"]
        dataset_response = await auth_client.post(
            "/api/benchmark-datasets",
            json={
                "profile_id": profile_id,
                "dataset_id": receipt["dataset_id"],
                "revision": receipt["revision"],
            },
        )
        assert dataset_response.status_code == 201, dataset_response.text
        dataset_id = dataset_response.json()["id"]
        cases = await auth_client.get(f"/api/benchmark-datasets/{dataset_id}/cases")
        case_id = cases.json()["cases"][0]["id"]
        selection = {
            "profile_id": profile_id,
            "dataset_revision_id": dataset_id,
            "case_id": case_id,
            "harness_profile_id": str(harness.id),
        }
        preflight = await auth_client.post("/api/benchmarks/preflight", json=selection)
        assert (
            preflight.status_code == 200
            and preflight.json()["configured"]
            and preflight.json()["compatible"]
        ), preflight.text
        store = get_trial_store()
        evidence = evidence_store(store)
        if not await asyncio.to_thread(evidence.client.bucket_exists, evidence.bucket):
            await asyncio.to_thread(evidence.client.make_bucket, evidence.bucket)
        limits = {
            "wall_time_seconds": 240,
            "memory_mb": 2048,
            "cpu_cores": 1.25,
            "disk_mb": 64,
            "model_requests": 4,
        }
        experiment = None
        if mode == "work":
            response = await auth_client.post(
                "/api/benchmark-trials",
                json={
                    "request_id": str(uuid.uuid4()),
                    **selection,
                    "limits": limits,
                    "agent_max_turns": 8,
                },
            )
            assert response.status_code == 202, response.text
            trial_id, job_id = (
                uuid.UUID(response.json()["id"]),
                uuid.UUID(response.json()["job_id"]),
            )
        else:
            response = await auth_client.post(
                "/api/experiments",
                json={
                    "name": "Native product matrix",
                    "dataset": {
                        "source": "benchmark_sdk",
                        "profile_id": profile_id,
                        "dataset_revision_id": dataset_id,
                        "case_ids": [case_id],
                    },
                    "configurations": [
                        {
                            "harness_profile_id": str(harness.id),
                            "limits": limits,
                            "agent_max_turns": 8,
                        }
                    ],
                    "n_runs_per_cell": 1,
                },
            )
            assert response.status_code == 201, response.text
            experiment = await db_session.get(Experiment, uuid.UUID(response.json()["id"]))
            await matrix.start_experiment(db_session, experiment)
            await matrix.advance_experiment(db_session, experiment)
            job = await db_session.scalar(select(BenchmarkJob))
            trial_id, job_id = job.trial_id, job.id
        diagnostics = []

        def observed(stage, error):
            nested = error.__context__
            chain = []
            while nested is not None and len(chain) < 5:
                chain.append(
                    {
                        "type": type(nested).__name__,
                        "frames": [
                            (Path(frame.filename).name, frame.name, frame.lineno)
                            for frame in traceback.extract_tb(nested.__traceback__)
                        ],
                    }
                )
                nested = nested.__context__
            diagnostics.append(
                {
                    "context": chain,
                    "stage": stage,
                    "code": getattr(error, "code", None),
                    "type": type(error).__name__,
                    "frames": [
                        (Path(frame.filename).name, frame.name, frame.lineno)
                        for frame in traceback.extract_tb(error.__traceback__)
                    ],
                }
            )

        class ObservedRuntime(ProductRuntime):
            async def execution(self, *args, **kwargs):
                try:
                    return await super().execution(*args, **kwargs)
                except Exception as error:
                    observed("execution", error)
                    raise

            async def evaluation(self, *args, **kwargs):
                try:
                    return await super().evaluation(*args, **kwargs)
                except Exception as error:
                    observed("evaluation", error)
                    raise

        from evalhive_harness.client import ConnectorClient

        original_request = ConnectorClient.request

        async def connector_request(self, method, *args, **kwargs):
            try:
                return await original_request(self, method, *args, **kwargs)
            except Exception as error:
                observed("connector." + method, error)
                raise

        monkeypatch.setattr(ConnectorClient, "request", connector_request)
        from app.benchmarks.harness_api import BenchmarkAttemptAPI

        original_prepare = BenchmarkAttemptAPI.prepare

        async def prepare_attempt(self, *args, **kwargs):
            try:
                return await original_prepare(self, *args, **kwargs)
            except Exception as error:
                observed("attempt.prepare", error)
                raise

        monkeypatch.setattr(BenchmarkAttemptAPI, "prepare", prepare_attempt)

        def factory(*args, **kwargs):
            try:
                return ObservedRuntime.installed(*args, **kwargs)
            except Exception as error:
                observed("construction", error)
                raise

        assert await asyncio.wait_for(
            run_execution_job(store, job_id, runtime_factory=factory), 300
        )
        public = await auth_client.get(f"/api/benchmark-trials/{trial_id}")
        data = public.json()
        job = await db_session.get(BenchmarkJob, job_id, populate_existing=True)
        if diagnostics:
            print(json.dumps({"runtime_diagnostics": diagnostics}))
        if job.status != "completed":
            task = await db_session.get(Task, uuid.UUID(data["task_id"]), populate_existing=True)
            if task.current_attempt_id is not None:
                attempt = await auth_client.get(f"/api/attempts/{task.current_attempt_id}")
                print(
                    json.dumps(
                        {
                            "attempt_diagnostics": {
                                key: attempt.json().get(key)
                                for key in ("status", "result", "completeness")
                            }
                        }
                    ),
                    flush=True,
                )
            journal = await auth_client.get(f"/api/benchmark-trials/{trial_id}/events?limit=500")
            print(
                json.dumps(
                    {
                        "lifecycle_diagnostics": {
                            "states": {
                                name: data[name]
                                for name in (
                                    "phase",
                                    "execution_status",
                                    "evaluation_status",
                                    "recording_status",
                                    "cleanup_status",
                                )
                            },
                            "events": [
                                {"event": event["event"], "data": event["data"]}
                                for event in journal.json()["events"]
                                if event["event"]
                                in {
                                    "controller.failed",
                                    "controller.stopping",
                                    "cleanup.failed",
                                    "capture.failed",
                                    "evaluation.finished",
                                }
                            ],
                            "model_requests": len(endpoint.requests),
                        }
                    }
                )
            )
        assert job.status == "completed", {
            "diagnostics": diagnostics,
            "status": job.status,
            "code": job.error_code,
            "trial": data,
        }
        assert all(
            data[name] == value
            for name, value in {
                "phase": "finished",
                "execution_status": "success",
                "evaluation_status": "completed",
                "recording_status": "complete",
                "cleanup_status": "complete",
            }.items()
        ), data
        assert len(endpoint.requests) == 2 and not endpoint.errors
        assert "never-send-to-agent" not in json.dumps(endpoint.requests)
        verdicts = await auth_client.get(f"/api/benchmark-trials/{trial_id}/evaluations")
        assert verdicts.json()[0]["status"] == "completed" and verdicts.json()[0]["success"] is True
        artifacts = await auth_client.get(f"/api/benchmark-trials/{trial_id}/artifacts")
        rows = artifacts.json()
        workspace_artifact = next(row for row in rows if row["artifact"]["id"] == "workspace")
        downloaded = await auth_client.get(
            f"/api/benchmark-trials/{trial_id}/artifacts/{workspace_artifact['id']}/download"
        )
        assert downloaded.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(downloaded.content)) as archive:
            assert (
                b"Driven through the Benchmark SDK"
                in archive.extractfile("workspace/result.txt").read()
            )
            assert archive.extractfile("workspace/agent-child.started").read() == b"started"
        task = await db_session.get(Task, uuid.UUID(data["task_id"]), populate_existing=True)
        assert task.status == "done" and task.current_attempt_id is not None
        leases = list(
            await db_session.scalars(
                select(BenchmarkResourceLease).where(BenchmarkResourceLease.trial_id == trial_id)
            )
        )
        assert leases and all(row.state == "released" for row in leases)
        if experiment is not None:
            await matrix.advance_experiment(db_session, experiment)
            await db_session.refresh(experiment)
            run = await db_session.scalar(select(ExperimentRun))
            assert (
                experiment.status == "completed"
                and run.status == "success"
                and run.external_verdict is True
            )
            assert run.weighted_score is None
        from app.benchmarks.regrade_dispatch import run_regrade_job
        from app.models.execution_attempt import ExecutionAttempt

        original_verdicts, original_sequence = verdicts.json(), data["next_seq"]
        original_attempt = task.current_attempt_id
        retry_request = {"request_id": str(uuid.uuid4())}
        retry_response = await auth_client.post(
            f"/api/benchmark-trials/{trial_id}/retry", json=retry_request
        )
        assert retry_response.status_code == 202, retry_response.text
        retried = retry_response.json()
        assert retried["retry_of_id"] == str(trial_id) and retried["snapshot"] == data["snapshot"]
        assert retried["task_id"] != data["task_id"]
        assert await asyncio.wait_for(
            run_execution_job(store, uuid.UUID(retried["job_id"]), runtime_factory=factory), 300
        )
        retry_state = (await auth_client.get(f"/api/benchmark-trials/{retried['id']}")).json()
        assert (
            retry_state["execution_status"] == "success"
            and retry_state["cleanup_status"] == "complete"
        ), retry_state
        retry_files = (
            await auth_client.get(f"/api/benchmark-trials/{retried['id']}/artifacts")
        ).json()
        retry_workspace = next(row for row in retry_files if row["artifact"]["id"] == "workspace")
        fresh_files = await auth_client.get(
            f"/api/benchmark-trials/{retried['id']}/artifacts/{retry_workspace['id']}/download"
        )
        assert fresh_files.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(fresh_files.content)) as archive:
            assert (
                archive.extractfile("workspace/result.txt").read()
                == b"Prepared through the Benchmark SDK\n"
            )
            assert "workspace/agent-child.started" not in archive.getnames()
        repeated = await auth_client.post(
            f"/api/benchmark-trials/{trial_id}/retry", json=retry_request
        )
        assert repeated.status_code == 202 and repeated.json()["id"] == retried["id"]
        assert len(endpoint.requests) == 3 and not endpoint.errors
        removed = await auth_client.delete(f"/api/providers/{provider.id}")
        assert removed.status_code == 204, removed.text
        request = {
            "request_id": str(uuid.uuid4()),
            "source_evaluation_id": original_verdicts[0]["id"],
        }
        queued = await auth_client.post(f"/api/benchmark-trials/{trial_id}/regrade", json=request)
        assert queued.status_code == 202, queued.text
        evaluation_job_id = uuid.UUID(queued.json()["id"])
        assert await asyncio.wait_for(
            run_regrade_job(store, evaluation_job_id, runtime_factory=factory), 180
        )
        evaluation_jobs = await auth_client.get(f"/api/benchmark-trials/{trial_id}/evaluation-jobs")
        if evaluation_jobs.json()[0]["status"] != "completed":
            from app.models.benchmark_evaluation_control import BenchmarkEvaluationJournal

            events = list(
                await db_session.execute(
                    select(
                        BenchmarkEvaluationJournal.event, BenchmarkEvaluationJournal.public_data
                    ).where(
                        BenchmarkEvaluationJournal.control_id == evaluation_job_id,
                        BenchmarkEvaluationJournal.event.in_(
                            [
                                "controller.failed",
                                "cleanup.failed",
                                "capture.failed",
                                "evaluation.finished",
                            ]
                        ),
                    )
                )
            )
            print(
                json.dumps(
                    {
                        "regrade_diagnostics": diagnostics,
                        "regrade_events": [
                            {"event": event, "data": payload} for event, payload in events
                        ],
                    }
                ),
                flush=True,
            )
        assert evaluation_jobs.json()[0]["status"] == "completed", evaluation_jobs.text
        assert evaluation_jobs.json()[0]["cleanup_status"] == "complete", evaluation_jobs.text
        graded = (await auth_client.get(f"/api/benchmark-trials/{trial_id}/evaluations")).json()
        assert len(graded) == 2 and graded[0] == original_verdicts[0], graded
        assert (
            graded[1]["kind"] == "regrade"
            and graded[1]["source_evaluation_id"] == original_verdicts[0]["id"]
        )
        assert graded[1]["status"] == "completed" and graded[1]["success"] is True, graded
        unchanged = (await auth_client.get(f"/api/benchmark-trials/{trial_id}")).json()
        assert unchanged == data and unchanged["next_seq"] == original_sequence
        await db_session.refresh(task)
        assert task.current_attempt_id == original_attempt and task.status == "done"
        attempts = list(
            await db_session.scalars(
                select(ExecutionAttempt.id).where(ExecutionAttempt.task_id == task.id)
            )
        )
        assert attempts == [original_attempt]
        assert len(endpoint.requests) == 3 and not endpoint.errors
        assert "never-send-to-agent" not in json.dumps(endpoint.requests)
        cold_leases = list(
            await db_session.scalars(
                select(BenchmarkResourceLease)
                .where(BenchmarkResourceLease.trial_id == trial_id)
                .execution_options(populate_existing=True)
            )
        )
        assert cold_leases and all(row.state == "released" for row in cold_leases)
        assert credential not in json.dumps(data) and credential not in verdicts.text
        from evalhive_benchmark.archive import verify_archive

        aggregates = await auth_client.get(f"/api/benchmark-trials/{trial_id}/aggregates")
        assert aggregates.status_code == 200, aggregates.text
        assert len(aggregates.json()) == 2 and all(row["complete"] for row in aggregates.json()), (
            aggregates.text
        )
        exported = await auth_client.get(
            f"/api/benchmark-trials/{trial_id}/archive?include_native_inputs=true"
        )
        assert exported.status_code == 200, exported.text
        native_archive = exported.json()
        assert verify_archive(native_archive)["native_aggregate_count"] == 2
        assert credential not in exported.text and "never-send-to-agent" not in exported.text
        write(base / f"native-archive-{connector_id}.json", native_archive)
        public_archive = await auth_client.get(f"/api/benchmark-trials/{trial_id}/archive")
        assert public_archive.status_code == 200
        assert not verify_archive(public_archive.json())["native_recompute_inputs"]
        if experiment is not None:
            bundled = await auth_client.get(
                f"/api/experiments/{experiment.id}/bundle?include_native_inputs=true"
            )
            assert bundled.status_code == 200, bundled.text
            assert verify_archive(bundled.json())["valid"]
            assert (
                bundled.json()["sections"]["trials"][0]["sections"]["evaluations"]
                == native_archive["sections"]["evaluations"]
            )
        print(
            json.dumps(
                {
                    "connector": connector_id,
                    "mode": mode,
                    "trial": str(trial_id),
                    "job": str(job_id),
                    "status": job.status,
                    "model_requests": len(endpoint.requests),
                    "cleanup": data["cleanup_status"],
                    "regrade_job": str(evaluation_job_id),
                    "regrade_without_agent_connection": True,
                    "retry_trial": retried["id"],
                    "native_aggregate_receipts": len(aggregates.json()),
                    "native_archive_verified": True,
                }
            )
        )

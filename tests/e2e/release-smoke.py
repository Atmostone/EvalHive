"""Run inside the API container with a local fixture and the scheduler paused.

Provision a new engineering principal without cloning owner providers or opening
registration. Run both connectors serially, then remove only that fixture's rows,
objects and workspaces. No global settings or owner model connections are edited.
"""

import argparse
import asyncio
import io
import json
import shutil
import tarfile
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from app.auth.security import create_access_token
from app.config import get_settings
from app.database import async_session
from app.harnesses.catalog import get_manifest
from app.harnesses.profiles import seed_workspace_profile
from app.models.chat_message import ChatMessage
from app.models.event import AgentEvent
from app.models.harness_profile import HarnessProfile
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.registry_entry import RegistryEntry
from app.models.task import Task
from app.models.template import Template
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.runtime import get_process_runtime
from app.storage.minio_client import BUCKET, get_minio_client
from sqlalchemy import delete, select


async def native_checks(client, workspace_id, profiles, provider_id, dataset_id, trial_ids, checks):
    from app.benchmarks.catalog import load_catalog

    declaration = load_catalog()['evalhive-cases']
    folder = next(path.parent for path in Path(get_settings().benchmark_manifest_dir).glob('*/manifest.json')
                  if json.loads(path.read_text()).get('id') == declaration['id'])
    receipts = [json.loads(path.read_text()) for path in (folder / 'datasets').glob('*.json')]
    receipt = next(item for item in receipts if item['dataset_id'] == dataset_id)
    response = await client.post('/api/benchmark-profiles', json={
        'name': 'Release native fixture', 'connector_id': declaration['id'], 'connector_version': declaration['version'],
        'config': {'evaluation_mode': 'reference_exact', 'resources': [{'id': 'tools', 'kind': 'tools', 'config': {'names': ['bash']}}]},
    })
    response.raise_for_status()
    profile_id = response.json()['id']
    response = await client.post('/api/benchmark-datasets', json={
        'profile_id': profile_id, 'dataset_id': dataset_id, 'revision': receipt['revision'],
    })
    response.raise_for_status()
    dataset_revision_id = response.json()['id']
    response = await client.get(f'/api/benchmark-datasets/{dataset_revision_id}/cases')
    response.raise_for_status()
    case_id = response.json()['cases'][0]['id']
    for connector, harness_id in profiles.items():
        async with async_session() as db:
            model = LLMModel(provider_id=provider_id, display_name='Local native fixture', api_name=f'release-native-{uuid.uuid4().hex}-{connector}')
            db.add(model)
            await db.flush()
            harness = await db.get(HarnessProfile, harness_id)
            assert harness.workspace_id == workspace_id
            harness.model_id = model.id
            await db.commit()
        response = await client.post('/api/benchmark-trials', json={
            'request_id': str(uuid.uuid4()), 'profile_id': profile_id, 'dataset_revision_id': dataset_revision_id,
            'case_id': case_id, 'harness_profile_id': str(harness_id),
            'limits': {'wall_time_seconds': 240, 'memory_mb': 2048, 'cpu_cores': 2, 'disk_mb': 512, 'model_requests': 4},
            'agent_max_turns': 4,
        })
        response.raise_for_status()
        trial_id = uuid.UUID(response.json()['id'])
        trial_ids.append(trial_id)
        deadline = asyncio.get_running_loop().time() + 300
        while asyncio.get_running_loop().time() < deadline:
            response = await client.get(f'/api/benchmark-trials/{trial_id}')
            response.raise_for_status()
            trial = response.json()
            if trial['phase'] == 'finished':
                break
            await asyncio.sleep(0.5)
        else:
            raise TimeoutError('Native release trial did not settle')
        assert all(trial[name] == value for name, value in {
            'execution_status': 'success', 'evaluation_status': 'completed',
            'recording_status': 'complete', 'cleanup_status': 'complete',
        }.items()), 'Native release trial failed; inspect its scoped journal'
        response = await client.get(f'/api/benchmark-trials/{trial_id}/artifacts')
        response.raise_for_status()
        artifact = next(row for row in response.json() if row['artifact']['id'] == 'workspace')
        response = await client.get(f"/api/benchmark-trials/{trial_id}/artifacts/{artifact['id']}/download")
        response.raise_for_status()
        with tarfile.open(fileobj=io.BytesIO(response.content)) as archive:
            assert archive.extractfile('workspace/native-result.txt').read() == b'Benchmark release artifact\n'
        checks.append({'connector': connector, 'mode': 'benchmark_sdk', 'status': 'passed', 'orchestrator_calls': 0})


async def cleanup_native(client, workspace_id, trial_ids):
    from app.api.benchmark_trials import get_trial_store
    from app.benchmarks.evidence import reference
    from app.benchmarks.product_runtime import evidence_store
    from app.models.benchmark_artifact import BenchmarkArtifact
    from app.models.benchmark_job import BenchmarkJob
    from app.models.benchmark_trial import BenchmarkResourceLease, BenchmarkTrial

    for trial_id in trial_ids:
        response = await client.get(f'/api/benchmark-trials/{trial_id}')
        response.raise_for_status()
        if response.json()['phase'] != 'finished':
            response = await client.post(f'/api/benchmark-trials/{trial_id}/cancel')
            response.raise_for_status()
    deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < deadline:
        async with async_session() as db:
            leases = list(await db.scalars(select(BenchmarkResourceLease).where(BenchmarkResourceLease.workspace_id == workspace_id, BenchmarkResourceLease.released_at.is_(None))))
            trials = list(await db.scalars(select(BenchmarkTrial).where(BenchmarkTrial.workspace_id == workspace_id)))
            if not leases and all(row.phase == 'finished' and row.cleanup_status == 'complete' for row in trials):
                break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError('Native fixture cleanup is incomplete; retain its ownership records for recovery')
    evidence = evidence_store(get_trial_store())
    async with async_session() as db:
        rows = list(await db.scalars(select(BenchmarkArtifact).where(BenchmarkArtifact.workspace_id == workspace_id)))
        for row in rows:
            locator = await evidence.resolve(workspace_id, row.trial_id, reference(row), visibilities={'agent', 'user', 'evaluator', 'private'}, db=db)
            await asyncio.to_thread(evidence.client.remove_object, locator['bucket'], locator['key'])
        await db.execute(delete(BenchmarkJob).where(BenchmarkJob.workspace_id == workspace_id))
        await db.execute(delete(BenchmarkTrial).where(BenchmarkTrial.workspace_id == workspace_id))
        await db.commit()
    for trial_id in trial_ids:
        await asyncio.to_thread(shutil.rmtree, Path(get_settings().data_dir) / 'benchmark-jobs' / str(trial_id), True)


async def main(model_url, native_dataset_id=None):
    endpoint = urlsplit(model_url)
    if endpoint.scheme != 'http' or not (endpoint.hostname or '').startswith('evalhive-release-model-'):
        raise ValueError('Use the dedicated local release fixture')
    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    task_ids, checks, native_trial_ids = [], [], []
    created = False
    try:
        async with async_session() as db:
            user = User(id=user_id, email=f'release-{user_id.hex}@example.com', display_name='Release engineering', is_active=True)
            db.add(user)
            await db.flush()
            workspace = Workspace(id=workspace_id, name='Release engineering', slug=f'release-{workspace_id.hex}', created_by=user_id)
            db.add(workspace)
            await db.flush()
            db.add(WorkspaceMember(user_id=user_id, workspace_id=workspace_id, role='owner'))
            provider = Provider(workspace_id=workspace_id, name='Local engineering fixture', endpoint=model_url, api_key='local-engineering-key')
            db.add(provider)
            await db.flush()
            model = LLMModel(provider_id=provider.id, display_name='Local engineering fixture', api_name='fake-model')
            tool = RegistryEntry(workspace_id=workspace_id, name='file_write', kind='builtin', config={}, secrets={})
            db.add_all([model, tool])
            await db.flush()
            role = Template(workspace_id=workspace_id, name='Release engineering', description='Local fixture only',
                            soul_md='Use the declared file tool for the local engineering fixture.', tool_ids=[str(tool.id)],
                            execution_mode='direct', max_ram='2g', max_cpu=100000, timeout_minutes=2)
            db.add(role)
            pi = await seed_workspace_profile(db, workspace_id)
            pi.model_id, pi.config = model.id, {'autoCompaction': False, 'maxRetries': 0}
            opencode = HarnessProfile(workspace_id=workspace_id, name='Release OpenCode', connector_id='opencode',
                                      connector_version=get_manifest('opencode')['version'], model_id=model.id, config={})
            db.add(opencode)
            await db.flush()
            profiles = {'pi': pi.id, 'opencode': opencode.id}
            role_id = role.id
            provider_id = provider.id
            await db.commit()
            created = True
        headers = {'Authorization': 'Bearer ' + create_access_token(user_id, workspace_id), 'X-Workspace-Id': str(workspace_id)}
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8000', headers=headers, timeout=15) as client:
            for connector, profile_id in profiles.items():
                async with async_session() as db:
                    task = Task(workspace_id=workspace_id, title=f'EVALHIVE_RELEASE_SMOKE:{connector}-{uuid.uuid4().hex}',
                                description='Produce the local engineering artifact.', template_id=role_id,
                                harness_profile_id=profile_id, execution_mode='direct', max_retries=0,
                                run_config={'memory_mode': 'off', 'max_iterations': 3})
                    db.add(task)
                    await db.commit()
                    task_id = task.id
                    task_ids.append(task_id)
                response = await client.post(f'/api/tasks/{task_id}/start')
                response.raise_for_status()
                deadline = asyncio.get_running_loop().time() + 150
                while asyncio.get_running_loop().time() < deadline:
                    response = await client.get(f'/api/tasks/{task_id}')
                    response.raise_for_status()
                    current = response.json()
                    if current['status'] in ('done', 'failed') and current['results_archived']:
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise TimeoutError('Release attempt did not settle')
                assert current['status'] == 'done', 'Native engineering execution failed'
                assert current['orchestrator_usage'] == {} and current['orchestrator_cost_usd'] == 0
                assert current['cost_usd'] is None and len(current['result_files']) == 1
                attempt_response = await client.get(f"/api/attempts/{current['current_attempt_id']}")
                attempt_response.raise_for_status()
                attempt = attempt_response.json()
                assert attempt['status'] == 'success' and attempt['prepared_at'] is not None
                assert attempt['connector_id'] == connector and attempt['completeness']['event_delivery'] == 'contiguous'
                file_name = current['result_files'][0].removeprefix(f'results/{task_id}/')
                downloaded = await client.get(f'/api/tasks/{task_id}/files/{quote(file_name, safe="/")}')
                downloaded.raise_for_status()
                assert downloaded.content == b'Release engineering artifact\n'
                checks.append({'connector': connector, 'status': 'passed', 'artifact_bytes': len(downloaded.content),
                               'orchestrator_calls': 0, 'reported_cost': 'unknown'})
            if native_dataset_id is not None:
                await native_checks(client, workspace_id, profiles, provider_id, native_dataset_id, native_trial_ids, checks)
    finally:
        if created:
            if native_trial_ids:
                async with httpx.AsyncClient(base_url='http://127.0.0.1:8000', headers=headers, timeout=15) as client:
                    await cleanup_native(client, workspace_id, native_trial_ids)
            runtime = get_process_runtime()
            for row in await asyncio.to_thread(runtime.list_active, str(workspace_id)):
                await asyncio.to_thread(runtime.stop, row['container_id'], str(workspace_id), grace_seconds=0, remove=True)
            async with async_session() as db:
                records = list(await db.scalars(select(QualityRecord).where(QualityRecord.workspace_id == workspace_id)))
                tasks = list(await db.scalars(select(Task).where(Task.workspace_id == workspace_id)))
                paths = {record.record_s3_path for record in records if record.record_s3_path}
                paths.update(path for task in tasks for path in task.result_files or [])
                storage = get_minio_client()
                for path in paths:
                    await asyncio.to_thread(storage.remove_object, BUCKET, path)
                await db.execute(delete(QualityRecord).where(QualityRecord.workspace_id == workspace_id))
                await db.execute(delete(AgentEvent).where(AgentEvent.workspace_id == workspace_id))
                await db.execute(delete(ChatMessage).where(ChatMessage.workspace_id == workspace_id))
                await db.execute(delete(Task).where(Task.workspace_id == workspace_id))
                await db.execute(delete(Template).where(Template.workspace_id == workspace_id))
                await db.execute(delete(HarnessProfile).where(HarnessProfile.workspace_id == workspace_id))
                await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
            for task_id in task_ids:
                directory = Path(get_settings().data_dir) / 'workspaces' / str(task_id)
                await asyncio.to_thread(shutil.rmtree, directory, True)
                if directory.exists():
                    raise RuntimeError('Engineering workspace cleanup did not complete')
    return {'status': 'passed', 'checks': checks, 'cleanup': 'complete', 'external_model_calls': 0, 'scientific_experiments': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-url', required=True)
    parser.add_argument('--native-dataset-id')
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(main(arguments.model_url, arguments.native_dataset_id))))

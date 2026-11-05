"""Run inside the API container with a local fixture and the scheduler paused.

Provision a new engineering principal without cloning owner providers or opening
registration. Run both connectors serially, then remove only that fixture's rows,
objects and workspaces. No global settings or owner model connections are edited.
"""

import argparse
import asyncio
import json
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from app.auth.security import create_access_token
from app.config import get_settings
from app.database import async_session
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


async def main(model_url):
    endpoint = urlsplit(model_url)
    if endpoint.scheme != 'http' or not (endpoint.hostname or '').startswith('evalhive-release-model-'):
        raise ValueError('Use the dedicated local release fixture')
    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    task_ids, checks = [], []
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
                                      connector_version='0.1.0', model_id=model.id, config={})
            db.add(opencode)
            await db.flush()
            profiles = {'pi': pi.id, 'opencode': opencode.id}
            role_id = role.id
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
    finally:
        if created:
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
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(main(arguments.model_url))))

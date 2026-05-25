import json
import logging
import os
import uuid

import docker
from docker.errors import NotFound

from app.models.template import Template

logger = logging.getLogger(__name__)

AGENT_IMAGE = "spawnhive-agent:latest"
DOCKER_NETWORK = "spawnhive_spawnhive-net"
LABEL_PREFIX = "spawnhive"


def get_docker_client() -> docker.DockerClient:
    return docker.from_env()


def get_llm_env_vars(llm_settings: dict) -> dict:
    return {
        "OPENAI_API_KEY": llm_settings.get("llm_api_key", ""),
        "OPENAI_BASE_URL": llm_settings.get("llm_base_url", ""),
        "LLM_MODEL": llm_settings.get("llm_model", ""),
    }


def spawn_agent(
    task_id: str,
    task_description: str,
    template: Template,
    llm_settings: dict,
    workspace_id: str,
    agent_token: str,
    memory_context: str = "",
    extra_env: dict | None = None,
) -> str:
    """Spawn a Docker container for the agent. Returns container ID."""
    from app.config import get_settings
    settings = get_settings()

    client = get_docker_client()
    short_id = uuid.uuid4().hex[:8]
    container_name = f"spawnhive-{str(task_id)[:8]}-{short_id}"

    # Use host paths for volume mounts (docker-py talks to host Docker daemon)
    host_data_dir = settings.host_data_dir
    host_shared_dir = os.path.join(host_data_dir, "shared", str(workspace_id))
    host_workspaces_dir = os.path.join(host_data_dir, "workspaces")
    host_workspace_path = os.path.join(host_workspaces_dir, str(task_id))

    # Create workspace dir via container-local path
    local_workspace = os.path.join(settings.data_dir, "workspaces", str(task_id))
    os.makedirs(local_workspace, exist_ok=True)
    # Make sure per-workspace shared dir + empty placeholder files exist on the host,
    # otherwise Docker would create a directory at the bind target instead of binding a file.
    local_shared = os.path.join(settings.data_dir, "shared", str(workspace_id))
    os.makedirs(local_shared, exist_ok=True)
    for fname in ("rules.md", "memory.md"):
        fpath = os.path.join(local_shared, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w") as fh:
                fh.write("")

    env = {
        "TASK_ID": str(task_id),
        "TASK_DESCRIPTION": task_description,
        "WEBHOOK_URL": f"http://api:8000/api/agent-webhook/{task_id}",
        "SPAWNHIVE_AGENT_TOKEN": agent_token,
        "AGENT_SOUL": template.soul_md,
        "AGENT_TOOLS": json.dumps(template.tools),
        "MCP_SERVERS": json.dumps(template.mcp_servers or []),
        "AGENT_MEMORY_CONTEXT": memory_context or "",
        **get_llm_env_vars(llm_settings),
        **{k: str(v) for k, v in (extra_env or {}).items()},
    }

    volumes = {
        os.path.join(host_shared_dir, "rules.md"): {"bind": "/data/rules.md", "mode": "ro"},
        os.path.join(host_shared_dir, "memory.md"): {"bind": "/data/memory.md", "mode": "ro"},
        host_workspace_path: {"bind": "/workspace", "mode": "rw"},
    }

    container = client.containers.run(
        image=AGENT_IMAGE,
        name=container_name,
        environment=env,
        volumes=volumes,
        network=DOCKER_NETWORK,
        mem_limit=template.max_ram or "2g",
        cpu_period=100000,
        cpu_quota=template.max_cpu or 100000,
        detach=True,
        labels={
            f"{LABEL_PREFIX}.task_id": str(task_id),
            f"{LABEL_PREFIX}.template_id": str(template.id),
            f"{LABEL_PREFIX}.template_name": template.name,
            f"{LABEL_PREFIX}.workspace_id": str(workspace_id),
        },
    )

    logger.info(f"Spawned agent container {container.id[:12]} for task {task_id}")
    return container.id


def _container_in_workspace(container, workspace_id: str | None) -> bool:
    if workspace_id is None:
        return True
    return container.labels.get(f"{LABEL_PREFIX}.workspace_id") == str(workspace_id)


def kill_agent(container_id: str, workspace_id: str | None = None) -> bool:
    """Kill a specific agent container. If workspace_id is set, only kill if container is in it."""
    client = get_docker_client()
    try:
        container = client.containers.get(container_id)
        if not _container_in_workspace(container, workspace_id):
            logger.warning(f"Container {container_id[:12]} not in workspace {workspace_id}")
            return False
        container.stop(timeout=10)
        container.remove(force=True)
        logger.info(f"Killed agent container {container_id[:12]}")
        return True
    except NotFound:
        logger.warning(f"Container {container_id[:12]} not found")
        return False


def kill_all_agents(workspace_id: str | None = None) -> int:
    """Kill all spawnhive agent containers (optionally scoped to workspace). Returns count."""
    client = get_docker_client()
    filters = {"label": [f"{LABEL_PREFIX}.task_id"]}
    if workspace_id is not None:
        filters["label"] = filters["label"] + [f"{LABEL_PREFIX}.workspace_id={workspace_id}"]
    containers = client.containers.list(filters=filters, all=True)
    count = 0
    for container in containers:
        try:
            container.stop(timeout=5)
            container.remove(force=True)
            count += 1
        except Exception as e:
            logger.error(f"Failed to kill {container.id[:12]}: {e}")
    logger.info(f"Killed {count} agent containers (workspace={workspace_id})")
    return count


def list_agents(workspace_id: str | None = None) -> list[dict]:
    """List active spawnhive agent containers (optionally scoped to workspace)."""
    client = get_docker_client()
    filters = {"label": [f"{LABEL_PREFIX}.task_id"]}
    if workspace_id is not None:
        filters["label"] = filters["label"] + [f"{LABEL_PREFIX}.workspace_id={workspace_id}"]
    containers = client.containers.list(filters=filters)
    agents = []
    for c in containers:
        agents.append({
            "container_id": c.id,
            "name": c.name,
            "status": c.status,
            "task_id": c.labels.get(f"{LABEL_PREFIX}.task_id"),
            "template_id": c.labels.get(f"{LABEL_PREFIX}.template_id"),
            "template_name": c.labels.get(f"{LABEL_PREFIX}.template_name"),
            "workspace_id": c.labels.get(f"{LABEL_PREFIX}.workspace_id"),
            "created": c.attrs.get("Created", ""),
        })
    return agents


def get_agent_stats(container_id: str, workspace_id: str | None = None) -> dict | None:
    """Get stats for a specific container; returns None if not found or wrong workspace."""
    client = get_docker_client()
    try:
        container = client.containers.get(container_id)
        if not _container_in_workspace(container, workspace_id):
            return None
        return {
            "container_id": container.id,
            "name": container.name,
            "status": container.status,
            "task_id": container.labels.get(f"{LABEL_PREFIX}.task_id"),
            "template_name": container.labels.get(f"{LABEL_PREFIX}.template_name"),
            "workspace_id": container.labels.get(f"{LABEL_PREFIX}.workspace_id"),
        }
    except NotFound:
        return None


def _agent_url(container_id: str) -> str | None:
    """Resolve container.name → http://<name>:8080 (Docker DNS)."""
    client = get_docker_client()
    try:
        container = client.containers.get(container_id)
        return f"http://{container.name}:8080"
    except NotFound:
        return None


async def get_agent_health(container_id: str) -> dict | None:
    import httpx

    base = _agent_url(container_id)
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/health")
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning(f"agent_health failed for {container_id[:12]}: {e}")
    return None


async def send_feedback(container_id: str, message: str) -> bool:
    import httpx

    base = _agent_url(container_id)
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{base}/feedback", json={"message": message})
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"send_feedback failed for {container_id[:12]}: {e}")
        return False


async def switch_agent_model(container_id: str, config: dict) -> bool:
    import httpx

    base = _agent_url(container_id)
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{base}/switch_model", json=config)
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"switch_agent_model failed for {container_id[:12]}: {e}")
        return False


async def abort_agent(container_id: str, reason: str) -> bool:
    import httpx

    base = _agent_url(container_id)
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{base}/abort", json={"reason": reason})
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"abort_agent failed for {container_id[:12]}: {e}")
        return False

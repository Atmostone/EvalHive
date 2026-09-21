"""Toolathlon executable-eval container mechanics (SPA-45 → runner).

The Docker half of running a Toolathlon benchmark case inside the Experiment
Runner: seed the agent workspace, run the case's ``preprocess`` and
``evaluation`` scripts in ``toolathlon-pack`` containers against the shared
``toolathlon_pg``, and read the binary verdict (eval exit code ``0`` == pass).

Ported from the host-side ``research/scripts/toolathlon_pilot.py`` — same
placeholder substitution, same single ``launch_time`` reused for both phases,
same "unconverted data remains" gym data-quirk fallback — but driven by the
docker SDK (``docker.sock``) instead of the ``docker`` CLI, so the scheduler
tick can spawn a *detached* container and re-inspect it on a later tick without
ever blocking.

No DB access: callers persist the returned container ids / verdict onto the
``ExperimentRun`` row. Both preprocess and eval containers are detached and
polled via :func:`poll_exit`; containers are removed at the run's terminal
settle (a long-running preprocess that keeps serving mocks during the agent run
is left alive until then — mirroring the host-script).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

PACK_IMAGE = "toolathlon-pack:latest"
NETWORK = "evalhive_evalhive-net"
PG_ENV = {
    "PGHOST": "toolathlon_pg",
    "PGPORT": "5432",
    "PGUSER": "eigent",
    "PGPASSWORD": "camel",
    "PGDATABASE": "toolathlon_gym",
}
GYM_MOUNT = "/gym"
WS_MOUNT = "/agent_ws"
PRE_RES_LOG = "/agent_ws/.pre_log.json"
EVAL_RES_LOG = "/agent_ws/.eval_log.json"


def _client():
    import docker

    return docker.from_env()


def _settings():
    from app.config import get_settings

    return get_settings()


def gym_host_path() -> str:
    """Host path of the toolathlon_gym clone, used for the ``-v <gym>:/gym``
    bind (docker-py talks to the host daemon, so this must be a host path)."""
    p = _settings().toolathlon_gym_path
    if not p:
        raise RuntimeError("TOOLATHLON_GYM_PATH is not configured")
    return os.path.abspath(os.path.expanduser(p))


def workspace_host_path(task_id) -> str:
    return os.path.join(_settings().host_data_dir, "workspaces", str(task_id))


def ensure_workspace_dir(task_id) -> str:
    """Create the container-local workspace dir so the host bind mount targets a
    real directory (mirrors ``docker_manager.spawn_agent``)."""
    local = os.path.join(_settings().data_dir, "workspaces", str(task_id))
    os.makedirs(local, exist_ok=True)
    return local


def launch_time_pair() -> tuple[str, str]:
    """The canonical ``%Y-%m-%d %H:%M:%S %A`` launch_time and its short fallback
    (a handful of finalpool scripts parse the timestamp without ``%A`` — a gym
    data-quality quirk handled by retrying preprocess with the short form)."""
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S %A"), now.strftime("%Y-%m-%d %H:%M:%S")


def substitute(cmd: str, *, gt: str | None, launch_time: str, res_log: str) -> str:
    """Resolve the case command placeholders for in-container execution."""
    cmd = cmd.replace("${TOOLATHLON_GYM_PATH}", GYM_MOUNT)
    cmd = cmd.replace("${AGENT_WORKSPACE}", WS_MOUNT)
    if gt is not None:
        cmd = cmd.replace("${GROUNDTRUTH_WORKSPACE}", gt)
    cmd = cmd.replace("${LAUNCH_TIME}", f"'{launch_time}'")
    cmd = cmd.replace("${RES_LOG_FILE}", res_log)
    if cmd.startswith("python "):
        cmd = "/opt/venv/bin/python " + cmd[len("python ") :]
    return cmd


def _seed_prefix(task_path: str) -> str:
    """Shell prefix copying the case's ``initial_workspace`` into the agent
    workspace before preprocess runs (the host-script does this as a separate
    ``cp`` step on the host)."""
    return (
        f"cp -a {GYM_MOUNT}/{task_path}/initial_workspace/. {WS_MOUNT}/ "
        "2>/dev/null || true; "
    )


def _remove_name(name: str) -> None:
    try:
        _client().containers.get(name).remove(force=True)
    except Exception:
        pass


def _run_pack(
    name: str, task_id, task_path: str, command: str, pg_host: str | None = None
) -> str:
    """Start a detached ``toolathlon-pack`` container; return its id. The gym
    (host path) and the task workspace (host path) are bind-mounted; ``PG_ENV``
    points every PG-backed script/server at ``toolathlon_pg``. ``pg_host`` (SPA-69)
    overrides ``PGHOST`` to pin this container at an isolated per-lane
    ``toolathlon_pg_lane_<i>`` instance; default keeps the shared ``toolathlon_pg``."""
    ensure_workspace_dir(task_id)
    _remove_name(name)
    env = dict(PG_ENV)
    if pg_host:
        env["PGHOST"] = pg_host
    container = _client().containers.run(
        image=PACK_IMAGE,
        name=name,
        command=["/bin/sh", "-c", command],
        network=NETWORK,
        volumes={
            gym_host_path(): {"bind": GYM_MOUNT, "mode": "rw"},
            workspace_host_path(task_id): {"bind": WS_MOUNT, "mode": "rw"},
        },
        environment=env,
        working_dir=f"{GYM_MOUNT}/{task_path}",
        detach=True,
        labels={"evalhive.toolathlon": "1", "evalhive.task_id": str(task_id)},
    )
    return container.id


def preprocess_container_name(task_id) -> str:
    """Deterministic name of a case's preprocess container — also the netns target
    a portal-case agent attaches to (``--network container:<name>``)."""
    return f"tlpre-{str(task_id)[:8]}"


def start_preprocess(
    task_id,
    task_path: str,
    preprocess_command: str,
    launch_time: str,
    *,
    keep_alive: bool = False,
    pg_host: str | None = None,
) -> str:
    """Seed the workspace + run the case's preprocess, detached. Returns the
    container id (poll it with :func:`poll_exit`).

    ``keep_alive`` (portal cases): after a SUCCESSFUL preprocess the container is
    held open (``&& tail -f /dev/null``) so the mock ``localhost:PORT`` server it
    started keeps running and its network namespace persists for the agent to
    join. A failed preprocess still exits non-zero (the ``&&`` short-circuits),
    so failure detection is preserved. Removed at the eval settle."""
    cmd = substitute(
        preprocess_command, gt=None, launch_time=launch_time, res_log=PRE_RES_LOG
    )
    if keep_alive:
        cmd = f"{cmd} && tail -f /dev/null"
    cmd = _seed_prefix(task_path) + cmd
    return _run_pack(
        preprocess_container_name(task_id), task_id, task_path, cmd, pg_host=pg_host
    )


def start_eval(
    task_id,
    task_path: str,
    eval_command: str,
    groundtruth_path: str | None,
    launch_time: str,
    *,
    pg_host: str | None = None,
) -> str:
    """Run the case's evaluation script, detached. Returns the container id;
    a clean exit code of ``0`` is a pass."""
    gt = f"{GYM_MOUNT}/{groundtruth_path}" if groundtruth_path else None
    cmd = substitute(eval_command, gt=gt, launch_time=launch_time, res_log=EVAL_RES_LOG)
    return _run_pack(f"tlev-{str(task_id)[:8]}", task_id, task_path, cmd, pg_host=pg_host)


def poll_exit(container_id: str) -> tuple[int | None, str]:
    """``(exit_code, logs_tail)`` once the container exited; ``(None, "")`` while
    it is still running. Raises ``docker.errors.NotFound`` if the container is
    gone (caller treats that as an infra error)."""
    c = _client().containers.get(container_id)
    c.reload()
    if c.status not in ("exited", "dead"):
        return None, ""
    code = int((c.attrs.get("State") or {}).get("ExitCode") or 0)
    logs = c.logs(tail=40).decode("utf-8", "replace")
    return code, logs[-2000:]


def has_unconverted_data_error(logs: str | None) -> bool:
    return "unconverted data remains" in (logs or "")


def remove(container_id: str | None) -> None:
    """Force-remove a container by id, best-effort (host-script ``docker rm -f``)."""
    if not container_id:
        return
    try:
        _client().containers.get(container_id).remove(force=True)
    except Exception as e:  # NotFound / API error — already gone is fine
        logger.warning(f"toolathlon: remove {str(container_id)[:12]} failed: {e}")

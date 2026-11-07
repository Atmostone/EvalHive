"""Keep native benchmark tasks on their owned lifecycle and immutable inputs."""

from app.models.task import Task


def is_benchmark_task(task):
    return bool((task.run_config or {}).get("benchmark_job_id"))


def ordinary_tasks():
    return Task.run_config["benchmark_job_id"].astext.is_(None)


def require_ordinary_task(task):
    from fastapi import HTTPException

    if is_benchmark_task(task):
        raise HTTPException(409, "Use the benchmark trial controls for this immutable native run")

"""Keep native benchmark tasks on their owned lifecycle and immutable inputs."""

from app.models.task import Task


def is_benchmark_task(task):
    return bool((getattr(task, "run_config", None) or {}).get("benchmark_job_id"))


def is_legacy_benchmark_task(task):
    config = getattr(task, "run_config", None) or {}
    suite = getattr(task, "benchmark_suite", None) or ""
    return bool(
        config.get("toolathlon_pilot")
        or config.get("environment_id") == "toolathlon"
        or config.get("pg_host")
        or (getattr(task, "benchmark_case_id", None) and suite and not suite.startswith("exp:"))
    )


def ordinary_tasks():
    return Task.run_config["benchmark_job_id"].astext.is_(None)


def require_ordinary_task(task):
    from fastapi import HTTPException

    if is_benchmark_task(task):
        raise HTTPException(409, "Use the benchmark trial controls for this immutable native run")

"""Behavioral / objective evaluation (E-04).

Runs a deterministic *static-analysis* probe over a task's produced code
artifacts and folds a single 0-10 measurement into the E-02 quality profile.

POC scope (decided with the user): Python-only, static-only probes that *parse*
the agent's code (never execute it), run in-process inside the backend:

- ``lint``  — ``ruff``: fewer lint findings per 100 LOC ⇒ higher score.
- ``types`` — ``mypy``: fewer type errors per 100 LOC ⇒ higher score.

Executing agent-produced code (pytest/jest) is intentionally *out of scope* — it
runs untrusted code and needs container isolation, a separate follow-up task.

Like the judge/reference evaluators, ``evaluate_objective_dimension`` never
raises: no Python artifacts ⇒ ``status: "skipped"``; any probe failure (tool
missing, timeout, unparseable output) ⇒ ``status: "error"`` so one dimension can
never block the others. Scores are deterministic, so identical artifacts are
memoised by content hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import tempfile

from app.models.task import Task
from app.quality.judge import _MAX_SCALE

logger = logging.getLogger(__name__)

PROBES = ("lint", "types")
DEFAULT_PROBE = "lint"

_PROBE_TIMEOUT_S = 60
# A finding density (issues per 100 LOC) at or above this scores 0; 0 issues = 10.
_ZERO_AT_PER_100_LOC = 10.0
# Bound the in-process result cache so a long-lived process can't leak.
_CACHE_MAX = 256
_CACHE: dict[str, dict] = {}


def _density_score(findings: int, loc: int) -> int:
    """Map findings-per-100-LOC to a 0-10 score (0 findings ⇒ 10)."""
    loc = max(loc, 1)
    density = findings / loc * 100.0
    score = _MAX_SCALE * (1.0 - min(density, _ZERO_AT_PER_100_LOC) / _ZERO_AT_PER_100_LOC)
    return int(round(score))


def _count_loc(local_files: list[str]) -> int:
    """Count non-blank lines across the downloaded files."""
    total = 0
    for path in local_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                total += sum(1 for line in f if line.strip())
        except OSError:
            continue
    return total


async def _exec(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a probe subprocess with a timeout. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"probe timed out after {_PROBE_TIMEOUT_S}s")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _run_ruff(files: list[str], cwd: str) -> int:
    """Count ruff lint findings (stdout JSON array, read regardless of exit code)."""
    import json

    _, out, err = await _exec(["ruff", "check", "--output-format=json", *files], cwd)
    try:
        data = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        raise RuntimeError(f"ruff produced no parseable output: {(err or out)[:200]}")
    return len(data)


async def _run_mypy(files: list[str], cwd: str) -> int:
    """Count mypy type errors. ``--ignore-missing-imports`` drops noise from the
    agent's code being analysed in isolation, without its dependency environment."""
    _, out, _ = await _exec(
        [
            sys.executable, "-m", "mypy",
            "--ignore-missing-imports",
            "--no-error-summary",
            "--no-color-output",
            *files,
        ],
        cwd,
    )
    return sum(1 for line in out.splitlines() if ": error:" in line)


async def _run_probe(probe: str, local_files: list[str], cwd: str) -> tuple[int, int]:
    """Return (findings, loc) for ``probe`` over the downloaded files."""
    loc = _count_loc(local_files)
    if probe == "lint":
        findings = await _run_ruff(local_files, cwd)
    elif probe == "types":
        findings = await _run_mypy(local_files, cwd)
    else:  # guarded by the caller, but keep the dispatch total
        raise ValueError(f"unknown probe '{probe}'")
    return findings, loc


def _read_artifact(s3_path: str) -> bytes:
    """Fetch one result artifact from object storage as raw bytes."""
    from app.storage.minio_client import get_file_stream

    resp = get_file_stream(s3_path)
    try:
        return resp.read()
    finally:
        try:
            resp.close()
            resp.release_conn()
        except Exception:  # noqa: BLE001 — best-effort connection cleanup
            pass


def _content_hash(probe: str, blobs: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    h.update(probe.encode())
    for name in sorted(blobs):
        h.update(name.encode())
        h.update(b"\0")
        h.update(blobs[name])
        h.update(b"\0")
    return h.hexdigest()


async def evaluate_objective_dimension(dim: dict, task: Task) -> dict:
    """Score one ``objective`` dimension. Never raises — errors become a result dict.

    Returns the same shape as ``judge._judge_dimension`` plus a ``skipped`` status
    when the task produced no Python artifacts for the probe to inspect.
    """
    try:
        probe = dim.get("probe") or DEFAULT_PROBE
        if probe not in PROBES:
            return {"status": "error", "score": None, "error": f"unknown probe '{probe}'"}

        code_paths = [str(p) for p in (task.result_files or []) if str(p).endswith(".py")]
        if not code_paths:
            return {"status": "skipped", "score": None}

        blobs = {os.path.basename(p): _read_artifact(p) for p in code_paths}
        digest = _content_hash(probe, blobs)
        if digest in _CACHE:
            return dict(_CACHE[digest])

        with tempfile.TemporaryDirectory(prefix="probe_") as tmp:
            local_files = []
            for name, data in blobs.items():
                fp = os.path.join(tmp, name)
                with open(fp, "wb") as f:
                    f.write(data)
                local_files.append(fp)
            findings, loc = await _run_probe(probe, local_files, tmp)

        result = {
            "status": "scored",
            "score": _density_score(findings, loc),
            "reasoning": f"{probe}: {findings} finding(s) over {loc} LOC",
            "input_tokens": 0,
            "output_tokens": 0,
        }
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[digest] = dict(result)
        return result
    except Exception as e:  # noqa: BLE001 — one dimension must not break the rest
        logger.warning(f"objective dimension '{dim.get('key')}' failed: {e}")
        return {"status": "error", "score": None, "error": str(e)[:300]}

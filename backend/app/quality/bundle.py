"""Frozen reproduction bundle (SPA-90).

A number that lives only in a private database is not a result — it is an anecdote.
This platform has already proved it: after a database replacement several headline
figures stopped being reproducible, because ``quality_records`` blobs live in MinIO
while the backup was a Postgres dump. Rows without the volume point into nothing.

A bundle is one self-contained archive that an outside reader — or the author six
months later — can recompute from, with a clean checkout, **no database, no object
store and no provider calls**.

Why that is possible at all: :func:`app.quality.experiment_report.build_report` is
pure, and all of its I/O lives in ``compute_report`` (fetch runs, fetch records,
compute calibration). So the offline recompute calls the PRODUCTION function rather
than a copy of it — a recompute script that reimplements κ proves the script works,
not the platform. The same split is applied to E-17 here (``pairs_from_rows``).

The report is also deterministic apart from a single field: ``generated_at`` is the
only wall-clock value in the report builder, and the bootstrap runs on a fixed
``BOOTSTRAP_SEED``. That is what makes an expected-output hash a check rather than
an impression — see :func:`canonical_report`.

**Best-effort input replay, not exact state replay.** The bundle pins what the
platform recorded: the rows, the profiles, the annotations, the archived blobs and
the E-20 snapshot of each run's inputs. It does NOT pin template versions, provider
endpoints or agent image digests, and it does not re-derive a judge profile from a
raw trace. The manifest says so in those words, so «reproduced» is never read as
more than it is.

Layout of a bundle directory (shipped as ``.tar.gz``)::

    manifest.json       what this is, what is inside, and what it does not pin
    experiment.json     the Experiment row
    runs.jsonl          ExperimentRun rows, as resolved by the frozen selection policy
    records.jsonl       QualityRecord rows — every profile blob
    annotations.jsonl   Annotation rows, each flagged `selected`
    expected.json       headline metrics + their hashes + the full-report hash
    blobs/records/      the MinIO record archives (always)
    blobs/logs/         the MinIO agent-log archives (only with --with-traces)
"""

from __future__ import annotations

import decimal
import hashlib
import json
import logging
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger(__name__)

BUNDLE_SCHEMA_VERSION = 1


class BundleMismatch(RuntimeError):
    """The offline recompute disagreed with the platform at export time.

    Raised instead of writing the archive: a bundle whose numbers do not already
    reproduce would hand a reader a verification that passes against itself while
    disagreeing with the stand it came from."""

    def __init__(self, message: str, diff: list[str] | None = None):
        super().__init__(message)
        self.diff = diff or []

# The one wall-clock field in a report. Dropped before hashing so two exports of
# the same data agree; nothing else is dropped, because anything else that moved
# is a real difference and the hash exists to catch it.
VOLATILE_REPORT_KEYS = ("generated_at",)

MANIFEST_NAME = "manifest.json"
EXPERIMENT_NAME = "experiment.json"
RUNS_NAME = "runs.jsonl"
RECORDS_NAME = "records.jsonl"
ANNOTATIONS_NAME = "annotations.jsonl"
EXPECTED_NAME = "expected.json"
RECORD_BLOB_DIR = "blobs/records"
LOG_BLOB_DIR = "blobs/logs"

# What this bundle promises, in the words it promises it. Carried in the manifest
# rather than left to a README, so it travels with the data.
REPLAY_KIND = "best_effort_input_replay"
REPLAY_NOT_PINNED = (
    "template_versions",
    "provider_endpoints",
    "agent_image_digests",
    "judge_profiles_are_stored_not_re_derived",
)

# What "recomputed" is allowed to mean here. Verification re-runs the report
# function over the rows this bundle carries — but that function reads the outcome
# score from a DENORMALIZED column (``experiment_runs.weighted_score``) rather than
# from the judge profile the score came from. So the raw summary, RQ2, the
# leaderboard and the significance matrix are replayed from a stored number, while
# the ``trusted`` view is genuinely re-derived from ``quality_profile.dimensions``
# and ``trajectory_profile.axes``.
#
# Stated in the manifest rather than left to be discovered: a reader who corrupts a
# profile dimension and watches only the raw summary would conclude the check does
# not work, when in fact they moved something the raw view never reads.
DERIVATION = {
    "recomputed_from_profiles": [
        "trusted.summary",
        "trusted.significance",
        "trusted.leaderboard",
        "axis_reliability",
        "outcome_axis_reliability",
        "calibration",
    ],
    "replayed_from_denormalized_run_columns": [
        "summary",
        "rq2",
        "judge_discrimination",
        "leaderboard",
        "significance",
    ],
    "note": (
        "the report reads the outcome score from experiment_runs.weighted_score, "
        "so those sections are re-run over a stored number rather than re-derived "
        "from the judge profile; the trusted view recomputes from the profiles"
    ),
}


# --------------------------------------------------------------------------- #
# Row (de)serialization
# --------------------------------------------------------------------------- #
# Driven off the SQLAlchemy mapper rather than a hand-written field list, ON
# PURPOSE. A hand-listed set drifts silently the first time a migration adds a
# column — which is the exact shape of the SPA-114 defect, where `extra: "ignore"`
# dropped an undeclared field between the wire and the database with no error
# anywhere. Reading the columns off the model means a new column either round-trips
# or fails loudly.

_TYPE_TAG = "__t"


def _encode(value: Any) -> Any:
    """JSON-safe form of one column value, keeping its type recoverable."""
    if isinstance(value, uuid.UUID):
        return {_TYPE_TAG: "uuid", "v": str(value)}
    if isinstance(value, decimal.Decimal):
        # str(), never float(): a Decimal that survives a round trip through binary
        # floating point is a different number, and these are money columns.
        return {_TYPE_TAG: "decimal", "v": str(value)}
    if isinstance(value, datetime):
        return {_TYPE_TAG: "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {_TYPE_TAG: "date", "v": value.isoformat()}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and _TYPE_TAG in value:
        kind, raw = value[_TYPE_TAG], value.get("v")
        if kind == "uuid":
            return uuid.UUID(raw)
        if kind == "decimal":
            return decimal.Decimal(raw)
        if kind == "datetime":
            return datetime.fromisoformat(raw)
        if kind == "date":
            return date.fromisoformat(raw)
        raise ValueError(f"unknown encoded type in bundle: {kind!r}")
    return value


def _column_names(model) -> list[str]:
    return [c.key for c in sa_inspect(model).columns]


def dump_row(obj) -> dict:
    """Every mapped column of one ORM row, JSON-safe."""
    return {name: _encode(getattr(obj, name)) for name in _column_names(type(obj))}


def load_row(model, data: dict):
    """Rebuild a **detached** instance — never added to a session.

    Detached ORM rows are already how this codebase moves archived data around:
    ``select_runs`` returns archived attempts as detached ``ExperimentRun``
    instances precisely so every consumer downstream stays unchanged. The same
    trick lets an offline recompute feed the real ``build_report``.

    A key the model does not have is an error, not a shrug: it means the bundle was
    written by a schema this checkout does not understand, and silently ignoring it
    would recompute a number from less data than the bundle carries.
    """
    known = set(_column_names(model))
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(
            f"{model.__name__}: bundle carries columns this checkout does not know: "
            f"{', '.join(unknown)}"
        )
    obj = model()
    for name in known:
        if name in data:
            setattr(obj, name, _decode(data[name]))
    return obj


def dump_rows(objs) -> str:
    """JSONL — one row per line, so a bundle streams and diffs line by line."""
    return "".join(
        json.dumps(dump_row(o), ensure_ascii=False, sort_keys=True) + "\n" for o in objs
    )


def load_rows(model, text: str) -> list:
    return [load_row(model, json.loads(line)) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Canonical form + hashing
# --------------------------------------------------------------------------- #


def canonical_report(report: dict) -> dict:
    """The report with its wall-clock field removed, and nothing else.

    Two exports of the same rows must hash identically, and the only thing standing
    in the way is ``generated_at``. Dropping more than that would let a real drift
    hide behind the exclusion list, which is the failure mode a hash exists to
    prevent."""
    return {k: v for k, v in report.items() if k not in VOLATILE_REPORT_KEYS}


def stable_json(obj: Any) -> str:
    """Sorted-key JSON, the form everything here is hashed from."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(stable_json(obj).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# The headline: what "reproduced" means
# --------------------------------------------------------------------------- #
# Two levels, because they answer different questions. `headline` is the contract —
# the metrics this bundle exists to let a reader check, and a mismatch there is a
# FAILURE. `full_report_sha256` is the tripwire: it covers everything else, and a
# mismatch is a WARNING with a diff. Without the first, a SCHEMA_VERSION bump would
# retroactively make every old bundle "unreproducible" though no number moved;
# without the second, drift anywhere outside the named metrics would pass unseen.

HEADLINE_METRICS = (
    "calibration",              # E-17 judge↔human κ per dimension + overall verdict
    "axis_reliability",         # the trajectory trust badges
    "outcome_axis_reliability", # the same traffic light on the outcome rubric
    "rq2",                      # the over-credit 2×2 at the pre-registered threshold
    "judge_discrimination",     # the threshold-free RQ2 headline (AUC)
    "summary",                  # per-config quality / trajectory / success rate
    "trusted",                  # what survives the gate — the claim the report makes
)


def headline_metrics(report: dict) -> dict:
    """The named subset, with each metric's own hash beside its value.

    The values ride along rather than only their digests: when a check fails, the
    reader has to be able to see WHAT moved without re-running an export."""
    out: dict[str, dict] = {}
    for key in HEADLINE_METRICS:
        value = report.get(key)
        out[key] = {"sha256": sha256_of(value), "value": value}
    return out


def compare_headline(expected: dict, actual: dict) -> list[dict]:
    """Metrics whose hash moved. Empty list means the contract held."""
    diffs: list[dict] = []
    for key in HEADLINE_METRICS:
        exp = (expected.get(key) or {}).get("sha256")
        act = (actual.get(key) or {}).get("sha256")
        if exp != act:
            diffs.append({"metric": key, "expected_sha256": exp, "actual_sha256": act})
    return diffs


def diff_paths(expected: Any, actual: Any, *, prefix: str = "", limit: int = 40) -> list[str]:
    """Key paths where two nested structures disagree — for the full-report warning.

    A hash tells a reader THAT something moved; this tells them where, which is the
    difference between a warning they can act on and one they learn to ignore."""
    out: list[str] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                walk(a.get(k), b.get(k), f"{path}.{k}" if path else str(k))
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                out.append(f"{path}: length {len(a)} → {len(b)}")
                return
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{i}]")
        elif a != b:
            out.append(f"{path}: {a!r} → {b!r}")

    walk(expected, actual, prefix)
    return out


# --------------------------------------------------------------------------- #
# Reading and writing the archive (pure)
# --------------------------------------------------------------------------- #


def write_tar(files: dict[str, bytes]) -> bytes:
    """A deterministic .tar.gz: sorted names, fixed mtime and mode.

    Two exports of the same slice have to be byte-identical, or an archive is not
    a thing worth hashing."""
    import gzip
    import io
    import tarfile

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    # mtime=0 on the gzip header too — otherwise the wrapper carries a timestamp
    # the tar inside was careful not to.
    return gzip.compress(raw.getvalue(), mtime=0)


def read_tar(data: bytes) -> dict[str, bytes]:
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        out = {}
        for m in tar.getmembers():
            if m.isfile():
                f = tar.extractfile(m)
                out[m.name] = f.read() if f else b""
        return out


def read_annotations(text: str):
    """Annotation rows plus the id set the database selected for calibration."""
    from app.models.annotation import Annotation

    rows, selected = [], set()
    for line in text.splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        picked = data.pop("selected", False)
        ann = load_row(Annotation, data)
        rows.append(ann)
        if picked:
            selected.add(ann.id)
    return rows, selected


def annotation_rows(annotations, selected_ids, records_by_task):
    """The ``(Annotation, QualityRecord)`` pairs E-17 shapes into calibration."""
    rows = []
    for ann in annotations:
        if ann.id in selected_ids:
            rec = records_by_task.get(ann.task_id)
            if rec is not None:
                rows.append((ann, rec))
    return rows


def verify_bundle(files: dict[str, bytes]) -> dict:
    """Recompute from the archive alone and compare. **No I/O of any kind.**"""
    from app.models.experiment import Experiment, ExperimentRun
    from app.models.quality_record import QualityRecord
    from app.quality.experiment_report import SCHEMA_VERSION as REPORT_SCHEMA_VERSION

    manifest = json.loads(files[MANIFEST_NAME])
    expected = json.loads(files[EXPECTED_NAME])

    exp = load_row(Experiment, json.loads(files[EXPERIMENT_NAME]))
    runs = load_rows(ExperimentRun, files[RUNS_NAME].decode())
    records = load_rows(QualityRecord, files[RECORDS_NAME].decode())
    by_task = {r.task_id: r for r in records}
    annotations, selected = read_annotations(files[ANNOTATIONS_NAME].decode())

    report = recompute_report(
        exp, runs, by_task,
        annotation_rows(annotations, selected, by_task),
        threshold_kappa=float(manifest["frozen_inputs"]["judge_calibration_min_kappa"]),
        method=manifest.get("method", "bt"),
        selection=manifest["selection"],
    )
    actual_headline = headline_metrics(report)
    headline_diffs = compare_headline(expected.get("headline") or {}, actual_headline)
    full_matches = sha256_of(canonical_report(report)) == expected.get("full_report_sha256")

    result = {
        "reproduced": not headline_diffs,
        "headline_diffs": headline_diffs,
        "full_report_matches": full_matches,
        "report_schema_version": {
            "bundle": expected.get("report_schema_version"),
            "checkout": REPORT_SCHEMA_VERSION,
        },
        "replay": manifest.get("replay"),
        "coverage": manifest.get("coverage"),
    }
    if not full_matches:
        exp_values = {k: v.get("value") for k, v in (expected.get("headline") or {}).items()}
        act_values = {k: v.get("value") for k, v in actual_headline.items()}
        result["full_report_diff"] = diff_paths(exp_values, act_values)
    return result


# --------------------------------------------------------------------------- #
# The offline pipeline
# --------------------------------------------------------------------------- #


def recompute_report(
    experiment,
    runs,
    records_by_task,
    annotation_rows,
    *,
    threshold_kappa: float,
    method: str,
    selection: str,
    partial: bool = False,
) -> dict:
    """E-17 pairs → calibration → report, with **no I/O of any kind**.

    Mirrors ``compute_report``'s body exactly, minus its three queries: it calls
    the same ``pairs_from_rows``, the same ``_compute_report`` and the same
    ``build_report``. Anything it reimplemented instead would make a passing
    verification a statement about this function rather than about the platform.

    ``annotation_rows`` are ``(Annotation, QualityRecord)`` pairs — the ones the
    database selected at export time.

    Note what is NOT here: ``config_drift`` and ``calibration_fingerprint`` are
    added by ``compute_report`` *after* ``build_report``, and both are statements
    about the live stand (does this config still resolve the same way? has anyone
    annotated since?). They cannot be recomputed from a frozen slice and are
    therefore carried in the manifest as frozen environment facts instead of being
    pretended into the reproducible surface."""
    from app.quality.experiment_report import build_report
    from app.quality.judge_calibration import _compute_report, pairs_from_rows

    calibration = None
    # Keyed on the runs having tasks at all, exactly as compute_report is: an
    # experiment whose runs carry tasks gets a calibration block even when nobody
    # annotated, because "measured, nothing to compare" and "not measured" are
    # different, and the report's `available` flag is how they differ.
    if any(getattr(r, "task_id", None) for r in runs):
        pairs = pairs_from_rows(annotation_rows)
        calibration = _compute_report(pairs, threshold_kappa=threshold_kappa)
        calibration["available"] = calibration.get("sample_size", 0) > 0

    return build_report(
        experiment,
        runs,
        records_by_task,
        method=method,
        partial=partial,
        calibration=calibration,
        selection=selection,
    )


# --------------------------------------------------------------------------- #
# E-20 coverage, aggregated
# --------------------------------------------------------------------------- #


def coverage_summary(records) -> dict:
    """What the bundle actually pins about each run's inputs.

    Read off the per-record E-20 snapshots (``quality_records.reproducibility``)
    rather than recomputed — the snapshot already carries an honest manifest of what
    the runtime exposes and what it does not, and restating it here would be a
    second opinion that can drift from the first.

    Reported so a reader sees the gap instead of assuming it away: a bundle whose
    snapshots are empty reproduces numbers, but not conditions."""
    n_records = 0
    n_with_snapshot = 0
    captured: dict[str, int] = {}
    missing: dict[str, int] = {}
    notes: dict[str, str] = {}
    for rec in records:
        n_records += 1
        snap = getattr(rec, "reproducibility", None)
        if not isinstance(snap, dict):
            continue
        manifest = snap.get("manifest") or {}
        n_with_snapshot += 1
        for field in manifest.get("captured") or []:
            captured[field] = captured.get(field, 0) + 1
        for field in manifest.get("missing") or []:
            missing[field] = missing.get(field, 0) + 1
        for field, note in (manifest.get("notes") or {}).items():
            notes.setdefault(field, note)
    return {
        "replay_kind": REPLAY_KIND,
        "not_pinned": list(REPLAY_NOT_PINNED),
        "n_records": n_records,
        "n_with_snapshot": n_with_snapshot,
        # A record with no snapshot is stated, not omitted: «no snapshot» and
        # «a snapshot that captured nothing» are different, and both are real.
        "n_without_snapshot": n_records - n_with_snapshot,
        "captured": dict(sorted(captured.items())),
        "missing": dict(sorted(missing.items())),
        "notes": dict(sorted(notes.items())),
    }


# --------------------------------------------------------------------------- #
# I/O: building a bundle from the database, and reading one back
# --------------------------------------------------------------------------- #
# Same layout as the report module: a pure core with one async loader beside it.
# Everything above this line runs without a database; everything below is the only
# part that needs one, and `verify_bundle` deliberately stays above it — a verifier
# that could reach the stand would not be verifying anything.


async def build_bundle(
    db,
    exp,
    *,
    selection: str,
    method: str = "bt",
    with_traces: bool = False,
) -> tuple[dict[str, bytes], dict]:
    """Freeze one experiment. Returns ``(files, manifest)``.

    Computes the report TWICE — once through the platform's own ``compute_report``
    and once through the offline path a reader will use — and raises if they
    disagree. A bundle that does not already reproduce is not a bundle, it is a
    claim, so it is never written."""
    import json as _json
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.api.settings import get_setting
    from app.models.annotation import Annotation
    from app.models.experiment import ExperimentRun
    from app.models.quality_record import QualityRecord
    from app.models.task import Task
    from app.quality.experiment_report import (
        SCHEMA_VERSION as REPORT_SCHEMA_VERSION,
    )
    from app.quality.experiment_report import compute_report, select_runs
    from app.quality.feedback import HUMAN_TYPES
    from app.quality.judge_calibration import DEFAULT_MIN_KAPPA

    runs = await select_runs(db, exp, selection=selection)
    task_ids = [r.task_id for r in runs if r.task_id]

    records = []
    annotations = []
    if task_ids:
        records = list(
            (
                await db.execute(
                    select(QualityRecord).where(QualityRecord.task_id.in_(task_ids))
                )
            ).scalars().all()
        )
        # Every annotation on these runs — superseded and machine-typed ones
        # included, because they are evidence a reader may want to audit. WHICH of
        # them the database selected for calibration is recorded per row, so the
        # offline path never re-implements the two SQL predicates behind that choice.
        annotations = list(
            (
                await db.execute(select(Annotation).where(Annotation.task_id.in_(task_ids)))
            ).scalars().all()
        )
    superseded = {a.supersedes_id for a in annotations if a.supersedes_id}
    selected_ids = {
        a.id for a in annotations
        if a.annotator_type in HUMAN_TYPES and a.id not in superseded
    }

    threshold = float(
        await get_setting(db, "judge_calibration_min_kappa", DEFAULT_MIN_KAPPA)
    )
    live = await compute_report(db, exp, method=method, selection=selection)

    tasks_by_id = {}
    if with_traces and task_ids:
        tasks_by_id = {
            t.id: t
            for t in (
                await db.execute(select(Task).where(Task.id.in_(task_ids)))
            ).scalars().all()
        }
    blobs = _fetch_blobs(records, tasks_by_id, with_traces=with_traces)

    exp_row = dump_row(exp)
    runs_text = dump_rows(runs)
    records_text = dump_rows(records)
    ann_text = "".join(
        _json.dumps(
            {**dump_row(a), "selected": a.id in selected_ids},
            ensure_ascii=False, sort_keys=True,
        ) + "\n"
        for a in annotations
    )

    # The offline answer, computed from the SERIALIZED form so a round-trip defect
    # cannot hide behind the live objects still sitting in memory.
    off_exp = load_row(type(exp), exp_row)
    off_runs = load_rows(ExperimentRun, runs_text)
    off_records = load_rows(QualityRecord, records_text)
    off_by_task = {r.task_id: r for r in off_records}
    off_annotations, off_selected = read_annotations(ann_text)
    offline = recompute_report(
        off_exp, off_runs, off_by_task,
        annotation_rows(off_annotations, off_selected, off_by_task),
        threshold_kappa=threshold, method=method, selection=selection,
    )

    # The only fields allowed to differ are the two `compute_report` adds AFTER
    # `build_report`; both describe the live stand rather than this slice.
    live_cmp = canonical_report(
        {k: v for k, v in live.items()
         if k not in ("config_drift", "calibration_fingerprint")}
    )
    off_cmp = canonical_report(offline)
    if sha256_of(live_cmp) != sha256_of(off_cmp):
        raise BundleMismatch(
            "refusing to write a bundle that does not reproduce the platform",
            diff_paths(live_cmp, off_cmp),
        )

    expected = {
        "headline": headline_metrics(offline),
        "full_report_sha256": sha256_of(off_cmp),
        "report_schema_version": REPORT_SCHEMA_VERSION,
    }
    manifest = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform_git_sha": _git_sha(),
        "experiment": {
            "id": str(exp.id),
            "name": exp_row.get("name"),
            "status": exp_row.get("status"),
            "revision": exp_row.get("revision"),
            "input_fingerprint": exp_row.get("input_fingerprint"),
        },
        "selection": selection,
        "method": method,
        "counts": {
            "runs": len(off_runs),
            "records": len(off_records),
            "annotations": len(off_annotations),
            "annotations_selected_for_calibration": len(off_selected),
            "record_blobs": sum(1 for k in blobs if k.startswith(RECORD_BLOB_DIR)),
            "log_blobs": sum(1 for k in blobs if k.startswith(LOG_BLOB_DIR)),
        },
        "blob_tiers": {
            "records": True,
            # Stated either way, so "no traces in this bundle" can never be read as
            # "this experiment had no traces".
            "logs": bool(with_traces),
            "logs_note": (
                "agent log archives are the bulk of the object store and are not "
                "needed to recompute any number; export with --with-traces to "
                "include them (CLI only — too large to stream over HTTP)"
            ),
        },
        # Frozen inputs the offline recompute needs and could not otherwise know.
        "frozen_inputs": {"judge_calibration_min_kappa": threshold},
        # Recorded, NOT recomputed: both describe the live stand at export time.
        "environment_at_export": {
            "config_drift": live.get("config_drift"),
            "calibration_fingerprint": live.get("calibration_fingerprint"),
            "note": (
                "not part of the reproducible surface — config_drift compares "
                "against models and templates as they are now, and the calibration "
                "fingerprint moves when anyone annotates"
            ),
        },
        "replay": {
            "kind": REPLAY_KIND,
            "not_pinned": list(REPLAY_NOT_PINNED),
            "derivation": DERIVATION,
        },
        "coverage": coverage_summary(off_records),
    }

    files = {
        MANIFEST_NAME: stable_json(manifest).encode(),
        EXPERIMENT_NAME: stable_json(exp_row).encode(),
        RUNS_NAME: runs_text.encode(),
        RECORDS_NAME: records_text.encode(),
        ANNOTATIONS_NAME: ann_text.encode(),
        EXPECTED_NAME: stable_json(expected).encode(),
        **blobs,
    }
    return files, manifest


def _fetch_blobs(records, tasks_by_id, *, with_traces: bool) -> dict[str, bytes]:
    """MinIO objects, keyed by their path inside the bundle.

    Record archives always: they are the canonical record, and exactly the half a
    Postgres dump loses. Agent logs only on request — they are the bulk, and no
    number depends on them (the profiles are already stored)."""
    from app.storage.minio_client import read_log_archive, read_quality_record

    blobs: dict[str, bytes] = {}
    for rec in records:
        if rec.record_s3_path:
            try:
                blobs[f"{RECORD_BLOB_DIR}/{rec.task_id}.json"] = read_quality_record(
                    rec.record_s3_path
                )
            except Exception as e:  # a missing object is reported, never silent
                logger.warning(f"record blob unreadable for task {rec.task_id}: {e}")
        if with_traces:
            path = getattr(tasks_by_id.get(rec.task_id), "log_archive_s3_path", None)
            if path:
                try:
                    blobs[f"{LOG_BLOB_DIR}/{rec.task_id}.bin"] = read_log_archive(path)
                except Exception as e:
                    logger.warning(f"log blob unreadable for task {rec.task_id}: {e}")
    return blobs


def _git_sha() -> str | None:
    """The checkout that produced this bundle, when there is one to name."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd="/app",
        )
        return out.stdout.strip() or None
    except Exception:
        return None

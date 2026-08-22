"""The frozen reproduction bundle (SPA-90).

A number that lives only in a private database is not a result. These tests pin the
two properties that make a bundle worth shipping: it reproduces the platform's own
numbers offline, and it *fails* when the data it carries has been altered. Without
the second, «verified» is decoration.
"""

import json
import uuid
from decimal import Decimal

import pytest

from app.models.annotation import Annotation
from app.models.experiment import Experiment, ExperimentRun
from app.models.quality_record import QualityRecord
from app.quality import bundle as B


# --- row round-trip ---------------------------------------------------------- #


def test_every_mapped_column_survives_the_round_trip():
    """Schema-driven, so a column added by a later migration is covered without
    anyone remembering to extend this test. A hand-written field list is exactly
    how SPA-114's `extra: "ignore"` dropped a value between the wire and the
    database with no error anywhere."""
    for model in (Experiment, ExperimentRun, QualityRecord, Annotation):
        names = B._column_names(model)
        assert names, f"{model.__name__} exposed no columns to the bundle"
        obj = model()
        back = B.load_row(model, B.dump_row(obj))
        for name in names:
            assert getattr(back, name) == getattr(obj, name), f"{model.__name__}.{name}"


def test_the_typed_columns_come_back_as_their_types_not_as_strings():
    """UUID, Decimal and datetime all serialize to strings, and a bundle that
    reloaded them as strings would still *look* fine — until an aggregate summed
    money as text or a report keyed runs by a string that never matches a UUID."""
    rec = QualityRecord()
    rec.task_id = uuid.uuid4()
    rec.cost_usd = Decimal("0.123456")
    back = B.load_row(QualityRecord, B.dump_row(rec))
    assert isinstance(back.task_id, uuid.UUID) and back.task_id == rec.task_id
    assert isinstance(back.cost_usd, Decimal) and back.cost_usd == rec.cost_usd
    # str() not float(): a Decimal that round-trips through binary floating point
    # is a different number, and these are money columns.
    assert str(back.cost_usd) == "0.123456"


def test_a_column_this_checkout_does_not_know_is_an_error_not_a_shrug():
    """A bundle written by a newer schema carries data this code cannot place.
    Ignoring it would recompute a number from less evidence than the bundle holds
    and report success."""
    data = B.dump_row(QualityRecord())
    data["a_column_from_the_future"] = 1
    with pytest.raises(ValueError, match="does not know"):
        B.load_row(QualityRecord, data)


# --- canonical form ---------------------------------------------------------- #


def test_only_the_wall_clock_field_is_dropped_before_hashing():
    """Two exports of the same rows must agree. Dropping more than `generated_at`
    would let a real drift hide behind the exclusion list — the failure the hash
    exists to prevent."""
    a = {"generated_at": "2026-01-01T00:00:00", "summary": {"x": 1}, "schema_version": 21}
    b = {"generated_at": "2026-08-22T09:00:00", "summary": {"x": 1}, "schema_version": 21}
    assert B.sha256_of(B.canonical_report(a)) == B.sha256_of(B.canonical_report(b))

    moved = {**b, "summary": {"x": 2}}
    assert B.sha256_of(B.canonical_report(a)) != B.sha256_of(B.canonical_report(moved))


def test_the_archive_is_byte_identical_for_the_same_slice():
    """An archive worth hashing has to be reproducible itself — otherwise every
    re-export looks like a change. Fixed mtime and mode, sorted names, and the gzip
    wrapper carries no timestamp the tar inside was careful to omit."""
    files = {"b.json": b'{"b":1}', "a.json": b'{"a":1}'}
    assert B.write_tar(files) == B.write_tar(dict(reversed(list(files.items()))))
    assert B.read_tar(B.write_tar(files)) == files


# --- what "reproduced" means ------------------------------------------------- #


def test_the_headline_is_the_contract_and_the_full_hash_is_the_tripwire():
    report = {
        "generated_at": "now", "schema_version": 21,
        "calibration": {"k": 1}, "axis_reliability": {"a": 1},
        "outcome_axis_reliability": {}, "rq2": {}, "judge_discrimination": {},
        "summary": {"per_config": []}, "trusted": {"available": False},
        "leaderboard": {"rows": []},
    }
    expected = B.headline_metrics(report)
    assert B.compare_headline(expected, B.headline_metrics(report)) == []

    # a headline metric moved → named, so a reader knows WHICH claim changed
    moved = {**report, "rq2": {"agreement": 0.9}}
    diffs = B.compare_headline(expected, B.headline_metrics(moved))
    assert [d["metric"] for d in diffs] == ["rq2"]

    # a NON-headline section moved → the headline still holds, and only the full
    # hash notices. That is the whole reason there are two levels: a schema bump
    # must not retroactively make every old bundle "unreproducible".
    elsewhere = {**report, "leaderboard": {"rows": [1]}}
    assert B.compare_headline(expected, B.headline_metrics(elsewhere)) == []
    assert B.sha256_of(B.canonical_report(report)) != B.sha256_of(
        B.canonical_report(elsewhere)
    )


def test_the_diff_names_where_it_moved_not_only_that_it_moved():
    """A warning a reader cannot act on is a warning they learn to ignore."""
    paths = B.diff_paths({"a": {"b": 1}, "c": [1, 2]}, {"a": {"b": 2}, "c": [1, 2]})
    assert paths == ["a.b: 1 → 2"]
    assert B.diff_paths({"c": [1]}, {"c": [1, 2]}) == ["c: length 1 → 2"]


# --- E-20 coverage ----------------------------------------------------------- #


def test_coverage_reports_what_the_snapshots_say_including_their_absence():
    """A bundle whose snapshots are empty reproduces numbers but not conditions,
    so the gap is stated rather than assumed away. «No snapshot» and «a snapshot
    that captured nothing» are different, and both are real."""
    with_snap = QualityRecord()
    with_snap.reproducibility = {
        "manifest": {
            "captured": ["model_api_name", "tools"],
            "missing": ["seed"],
            "notes": {"seed": "present only for benchmark-materialized runs"},
        }
    }
    without = QualityRecord()

    cov = B.coverage_summary([with_snap, with_snap, without])
    assert (cov["n_records"], cov["n_with_snapshot"], cov["n_without_snapshot"]) == (3, 2, 1)
    assert cov["captured"] == {"model_api_name": 2, "tools": 2}
    assert cov["missing"] == {"seed": 2}
    assert cov["notes"]["seed"].startswith("present only")
    # The honest name travels with the data, not in a README nobody opens.
    assert cov["replay_kind"] == "best_effort_input_replay"
    assert "template_versions" in cov["not_pinned"]


def test_the_manifest_says_which_sections_are_recomputed_and_which_are_replayed():
    """The report reads the outcome score from a denormalized run column, so the
    raw summary is re-run over a stored number while the trusted view is genuinely
    re-derived from the profiles. A reader who corrupts a profile dimension and
    watches only the raw summary would otherwise conclude the check is broken."""
    assert "trusted.summary" in B.DERIVATION["recomputed_from_profiles"]
    assert "summary" in B.DERIVATION["replayed_from_denormalized_run_columns"]
    assert "weighted_score" in B.DERIVATION["note"]


# --- the annotation selection travels with the rows -------------------------- #


def test_the_database_s_calibration_choice_is_carried_not_re_derived():
    """Which annotations feed E-17 is decided by two SQL predicates (current, and
    of a human type). The bundle records the DECISION per row, so the offline path
    never re-implements them — a second implementation of a filter is a second
    thing that can drift."""
    keep, drop = Annotation(), Annotation()
    keep.id, drop.id = uuid.uuid4(), uuid.uuid4()
    keep.task_id = drop.task_id = uuid.uuid4()
    text = "".join(
        json.dumps({**B.dump_row(a), "selected": sel}, sort_keys=True) + "\n"
        for a, sel in ((keep, True), (drop, False))
    )
    rows, selected = B.read_annotations(text)
    assert len(rows) == 2 and selected == {keep.id}

    rec = QualityRecord()
    pairs = B.annotation_rows(rows, selected, {keep.task_id: rec})
    assert [a.id for a, _ in pairs] == [keep.id]

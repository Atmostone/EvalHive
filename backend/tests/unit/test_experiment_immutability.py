"""Input fingerprint behind the experiment revision counter, SPA-84 (pure).

The fingerprint answers one question: has the thing being measured changed?
It must move when the matrix, the case set, the repetitions or the evaluation
settings change, and must stay put when only results accumulate — otherwise a
cached report is either served stale or thrown away on every tick.
"""

from app.models.experiment import Experiment
from app.quality.experiments import experiment_input_fingerprint


def _exp(**over) -> Experiment:
    base = dict(
        configurations=[
            {"config_key": "cfg-01", "fingerprint": "aaaa", "label": "A"},
            {"config_key": "cfg-02", "fingerprint": "bbbb", "label": "B"},
        ],
        dataset_cases=[{"case_key": "case-1"}, {"case_key": "case-2"}],
        n_runs_per_cell=1,
        eval_config={"trajectory": True},
    )
    base.update(over)
    return Experiment(**base)


def test_fingerprint_is_stable_for_the_same_inputs():
    assert experiment_input_fingerprint(_exp()) == experiment_input_fingerprint(_exp())


def test_config_and_case_order_do_not_matter():
    """Row order out of JSONB is not guaranteed; the fingerprint must not depend on it."""
    shuffled = _exp(
        configurations=[
            {"config_key": "cfg-02", "fingerprint": "bbbb", "label": "B"},
            {"config_key": "cfg-01", "fingerprint": "aaaa", "label": "A"},
        ],
        dataset_cases=[{"case_key": "case-2"}, {"case_key": "case-1"}],
    )
    assert experiment_input_fingerprint(shuffled) == experiment_input_fingerprint(_exp())


def test_label_change_alone_does_not_move_the_fingerprint():
    """A label is cosmetic — renaming a config must not invalidate a valid report."""
    relabelled = _exp(
        configurations=[
            {"config_key": "cfg-01", "fingerprint": "aaaa", "label": "renamed"},
            {"config_key": "cfg-02", "fingerprint": "bbbb", "label": "B"},
        ]
    )
    assert experiment_input_fingerprint(relabelled) == experiment_input_fingerprint(_exp())


def test_added_config_moves_the_fingerprint():
    added = _exp(
        configurations=_exp().configurations
        + [{"config_key": "cfg-03", "fingerprint": "cccc", "label": "C"}]
    )
    assert experiment_input_fingerprint(added) != experiment_input_fingerprint(_exp())


def test_retiring_a_config_moves_the_fingerprint():
    """The entry is kept rather than deleted, so the stamp is what must be noticed."""
    retired = _exp(
        configurations=[
            {"config_key": "cfg-01", "fingerprint": "aaaa", "label": "A"},
            {
                "config_key": "cfg-02",
                "fingerprint": "bbbb",
                "label": "B",
                "retired_at": "2026-08-12T00:00:00",
            },
        ]
    )
    assert experiment_input_fingerprint(retired) != experiment_input_fingerprint(_exp())


def test_changed_case_set_moves_the_fingerprint():
    assert experiment_input_fingerprint(
        _exp(dataset_cases=[{"case_key": "case-1"}])
    ) != experiment_input_fingerprint(_exp())


def test_changed_repetitions_move_the_fingerprint():
    assert experiment_input_fingerprint(
        _exp(n_runs_per_cell=3)
    ) != experiment_input_fingerprint(_exp())


def test_changed_eval_config_moves_the_fingerprint():
    assert experiment_input_fingerprint(
        _exp(eval_config={"trajectory": False})
    ) != experiment_input_fingerprint(_exp())


def test_results_do_not_move_the_fingerprint():
    """Inputs only: cost, status and a cached report accumulate as an experiment runs."""
    progressed = _exp()
    progressed.status = "completed"
    progressed.accumulated_cost_usd = 12
    progressed.report = {"schema_version": 13}
    assert experiment_input_fingerprint(progressed) == experiment_input_fingerprint(_exp())


def test_empty_experiment_fingerprints_without_error():
    assert experiment_input_fingerprint(
        Experiment(configurations=[], dataset_cases=[], n_runs_per_cell=1, eval_config={})
    )

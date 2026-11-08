"""Historical executable-evaluation metadata remains readable in frozen suites."""

from app.quality.experiments import _external_eval, cases_from_suite


def test_cases_from_suite_carries_external_eval_environment_meta():
    cases = cases_from_suite("toolathlon")
    assert cases, "toolathlon suite should contain cases"
    c = cases[0]
    ext = c.get("external_eval")
    assert ext and ext["preprocess_command"] and ext["eval_command"]
    assert c["environment"]["required_services"] == ["toolathlon_pg"]
    assert c["meta"]["task_path"]
    assert _external_eval(c) is not None


def test_external_eval_helpers_on_plain_case():
    assert _external_eval(None) is None
    assert _external_eval({"case_key": "x"}) is None
    # incomplete external_eval (missing eval_command) is ignored
    assert _external_eval({"external_eval": {"preprocess_command": "p"}}) is None

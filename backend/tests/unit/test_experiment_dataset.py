"""Dataset freezing for SPA-40 experiments (upload validation, suite, tasks)."""

import uuid
from types import SimpleNamespace

import pytest

from app.quality.experiments import (
    MAX_CASES,
    cases_from_suite,
    cases_from_tasks,
    cases_from_upload,
    normalize_dataset,
)


class TestUpload:
    def test_valid_cases_frozen_with_default_keys(self):
        frozen = cases_from_upload(
            [
                {"task_input": {"title": "T1", "description": "d1"}},
                {
                    "task_input": {"title": "T2"},
                    "case_id": "custom-7",
                    "reference_answer": "42",
                    "canonical_trajectory": ["search", "write"],
                },
            ]
        )
        assert frozen[0]["case_key"] == "upload-001"
        assert frozen[0]["title"] == "T1"
        assert frozen[0]["description"] == "d1"
        assert "reference_answer" not in frozen[0]
        assert frozen[1]["case_key"] == "custom-7"
        assert frozen[1]["reference_answer"] == "42"
        assert frozen[1]["canonical_trajectory"] == ["search", "write"]

    def test_missing_title_yields_clear_error(self):
        with pytest.raises(ValueError, match=r"case 1: task_input\.title"):
            cases_from_upload([{"task_input": {"description": "no title"}}])

    def test_inline_rubric_validated_and_frozen(self):
        frozen = cases_from_upload(
            [
                {
                    "task_input": {"title": "math"},
                    "rubric": {
                        "name": "Math",
                        "dimensions": [
                            {"key": "correct", "weight": 0.7, "threshold": 6, "critical": True},
                            {"key": "clarity", "weight": 0.3},
                        ],
                    },
                }
            ]
        )
        rubric = frozen[0]["rubric"]
        assert rubric["name"] == "Math"
        assert [d["key"] for d in rubric["dimensions"]] == ["correct", "clarity"]
        assert rubric["dimensions"][0]["critical"] is True
        # evaluator defaults to "judge" so the frozen case is self-contained
        assert rubric["dimensions"][0]["evaluator"] == "judge"

    def test_inline_rubric_bad_shape_yields_clear_error(self):
        with pytest.raises(ValueError, match=r"case 1: rubric\.dimensions"):
            cases_from_upload(
                [{"task_input": {"title": "x"}, "rubric": {"dimensions": []}}]
            )
        with pytest.raises(ValueError, match=r"case 1: rubric\.dimensions\.0\.weight"):
            cases_from_upload(
                [
                    {
                        "task_input": {"title": "x"},
                        "rubric": {"dimensions": [{"key": "a", "weight": 0}]},
                    }
                ]
            )

    def test_unknown_field_yields_clear_error(self):
        with pytest.raises(ValueError, match=r"case 1: referense_answer"):
            cases_from_upload(
                [{"task_input": {"title": "T"}, "referense_answer": "typo"}]
            )

    def test_non_object_line_rejected(self):
        with pytest.raises(ValueError, match="case 2: expected a JSON object"):
            cases_from_upload([{"task_input": {"title": "T"}}, "just a string"])

    def test_duplicate_case_id_rejected(self):
        with pytest.raises(ValueError, match="duplicate case_id 'dup'"):
            cases_from_upload(
                [
                    {"task_input": {"title": "A"}, "case_id": "dup"},
                    {"task_input": {"title": "B"}, "case_id": "dup"},
                ]
            )

    def test_empty_upload_rejected(self):
        with pytest.raises(ValueError, match="no cases"):
            cases_from_upload([])


class TestSuite:
    def test_freezes_existing_suite(self):
        frozen = cases_from_suite("capability-isolation")
        assert len(frozen) > 0
        assert all(c["case_key"] and c["title"] for c in frozen)

    def test_unknown_case_ids_rejected(self):
        with pytest.raises(ValueError, match="unknown case ids"):
            cases_from_suite("capability-isolation", case_ids=["nope-404"])

    def test_unknown_suite_raises(self):
        with pytest.raises(FileNotFoundError):
            cases_from_suite("no-such-suite")


class TestTasks:
    def _task(self, title="t", **kw):
        return SimpleNamespace(
            id=kw.get("id", uuid.uuid4()),
            title=title,
            description=kw.get("description"),
            reference_answer=kw.get("reference_answer"),
            canonical_trajectory=kw.get("canonical_trajectory"),
            capability_spec=kw.get("capability_spec"),
        )

    def test_snapshots_input_and_gold_fields(self):
        t = self._task(
            title="source", description="d", reference_answer="gold",
            capability_spec={"required_tools": ["bash"]},
        )
        frozen = cases_from_tasks([t])
        assert frozen[0]["case_key"] == f"task-{t.id.hex[:8]}"
        assert frozen[0]["title"] == "source"
        assert frozen[0]["reference_answer"] == "gold"
        assert frozen[0]["capability_spec"] == {"required_tools": ["bash"]}

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="matched no tasks"):
            cases_from_tasks([])


class TestNormalize:
    def test_dispatch_and_caps(self):
        cases = normalize_dataset(
            {"source": "upload", "cases": [{"task_input": {"title": "T"}}]}
        )
        assert len(cases) == 1

        too_many = [{"task_input": {"title": f"T{i}"}} for i in range(MAX_CASES + 1)]
        with pytest.raises(ValueError, match="too many cases"):
            normalize_dataset({"source": "upload", "cases": too_many})

    def test_unknown_source_rejected(self):
        with pytest.raises(ValueError, match="unknown dataset source"):
            normalize_dataset({"source": "ftp"})

    def test_suite_requires_suite_name(self):
        with pytest.raises(ValueError, match="dataset.suite is required"):
            normalize_dataset({"source": "benchmark_suite"})

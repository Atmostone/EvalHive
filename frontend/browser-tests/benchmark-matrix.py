"""Exercise native matrix creation and controls without ordinary judge/report calls."""

import importlib.util
import os
import threading
import uuid
from copy import deepcopy
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

spec = importlib.util.spec_from_file_location(
    "benchmark_work", Path(__file__).with_name("benchmark-work.py")
)
work = importlib.util.module_from_spec(spec)
spec.loader.exec_module(work)
EXPERIMENT = str(uuid.UUID(int=30))
CELL = str(uuid.UUID(int=31))
CLONE = str(uuid.UUID(int=33))


class StaticFiles(work.StaticFiles):
    def do_GET(self):
        if self.path.startswith("/experiments"):
            self.path = "/index.html"
        super().do_GET()


class Fixture(work.Fixture):
    def __init__(self):
        super().__init__()
        self.compatible = True
        self.experiment, self.controls, self.created = None, [], []
        self.rows = []
        self.history = []
        self.changes = []
        self.copy = None

    def route(self, route):
        parsed, request = urlsplit(route.request.url), route.request
        path = parsed.path[4:]
        if not parsed.path.startswith("/api/experiments"):
            assert not path.endswith("/report"), (
                "Native view must not request the ordinary statistical report"
            )
            return super().route(route)
        assert request.headers.get("x-workspace-id") == work.WORKSPACE
        body, result = request.post_data_json, None
        if path == "/experiments/preview":
            assert body["dataset"]["source"] == "benchmark_sdk" and body["max_parallel"] == 1
            result = {
                "n_configs": 1,
                "n_cases": 1,
                "n_runs_per_cell": 2,
                "total_runs": 2,
                "est_cost_usd": None,
                "est_duration_minutes": None,
                "warnings": [],
            }
        elif path == "/experiments" and request.method == "POST":
            self.created.append(deepcopy(body))
            assert "eval_config" not in body and "template_id" not in body["configurations"][0]
            self.experiment = {
                "id": EXPERIMENT,
                "name": body["name"],
                "description": None,
                "status": "draft",
                "dataset": body["dataset"],
                "n_cases": 1,
                "n_configs": 1,
                "n_runs_per_cell": 2,
                "total_runs": 2,
                "accumulated_cost_usd": 0,
                "budget_limit_usd": None,
                "max_parallel": 1,
                "revision": 1,
                "n_retired_configs": 0,
                "eval_config": {"mode": "native"},
                "configurations": [{"config_key": "cfg-01", "label": "Fixture Pi", "harness_profile_id": work.HARNESS}],
                "created_at": "2026-01-01T00:00:00Z",
                "completed_at": None,
                "matrix": [],
                "run_totals": {},
            }
            result = self.experiment
        elif path == "/experiments":
            result = [self.experiment] if self.experiment else []
        elif path == "/experiments/" + EXPERIMENT:
            result = self.experiment
        elif path == "/experiments/" + CLONE:
            result = self.copy
        elif path == f"/experiments/{CLONE}/results":
            result = []
        elif path == f"/experiments/{EXPERIMENT}/clone":
            self.changes.append(("clone", body))
            self.copy = {**deepcopy(self.experiment), "id": CLONE, "name": body["name"],
                         "status": "draft", "cloned_from_id": EXPERIMENT,
                         "cloned_from_revision": self.experiment["revision"], "clone_mode": "frozen"}
            result = self.copy
        elif path == f"/experiments/{EXPERIMENT}/add-config":
            assert body == {"harness_profile_id": work.HARNESS}
            self.changes.append(("add", body))
            self.experiment["configurations"].append({"config_key": "cfg-02", "label": "Added Pi"})
            self.experiment["n_configs"] = 2
            result = self.experiment
        elif path == f"/experiments/{EXPERIMENT}/configs/cfg-02":
            assert request.method == "DELETE"
            self.changes.append(("retire", "cfg-02"))
            self.experiment["configurations"][1]["retired_at"] = "2026-01-02T00:00:00Z"
            self.experiment.update(n_configs=1, n_retired_configs=1)
            result = self.experiment
        elif path == f"/experiments/{EXPERIMENT}/results":
            result = self.rows
        elif path == f"/experiments/{EXPERIMENT}/cells/{CELL}/attempts":
            result = self.history
        elif path.startswith(f"/experiments/{EXPERIMENT}/") and path.rsplit("/", 1)[-1] in {
            "run",
            "pause",
            "resume",
            "cancel",
            "retry-failed",
        }:
            action = path.rsplit("/", 1)[-1]
            self.controls.append(action)
            self.experiment["status"] = {
                "run": "running",
                "pause": "paused",
                "resume": "running",
                "cancel": "cancelled",
                "retry-failed": "running",
            }[action]
            if action == "retry-failed":
                self.experiment["revision"] += 1
                self.history = [
                    {
                        "id": str(uuid.UUID(int=32)),
                        "attempt_index": 1,
                        "task_id": work.TASK,
                        "task_available": True,
                        "status": "failed",
                        "external_verdict": None,
                        "cost_usd": None,
                        "failure_type": "infra",
                        "retired_reason": "retry",
                    }
                ]
            self.rows = [
                {
                    "id": CELL,
                    "config_key": "cfg-01",
                    "case_key": "native-case",
                    "run_index": 0,
                    "status": "pending" if action == "retry-failed" else "running",
                    "task_id": None if action == "retry-failed" else work.TASK,
                    "cost_usd": None,
                }
            ]
            result = self.experiment
        else:
            raise AssertionError("Unexpected native matrix endpoint: " + path)
        route.fulfill(json=deepcopy(result))


def main():
    dist = Path(os.environ["EVALHIVE_FRONTEND_DIST"]).resolve(strict=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticFiles, directory=str(dist)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixture, errors = Fixture(), []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            owner = work.settings.context(browser, "owner")
            owner.route("**/*", fixture.route)
            page = owner.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/experiments")
            page.get_by_role("button", name="New Experiment", exact=True).click()
            expect(page.get_by_role("button", name="Benchmark suite", exact=True)).to_have_count(0)
            expect(page.get_by_label("Toolathlon lanes", exact=True)).to_have_count(0)
            page.get_by_label("Experiment type", exact=True).select_option("benchmark_sdk")
            page.get_by_label("Native matrix name", exact=True).fill("Browser native matrix")
            page.get_by_label("Run benchmark profile", exact=True).select_option(work.PROFILE)
            page.get_by_label("Run dataset revision", exact=True).select_option(work.DATASET)
            page.get_by_label("Run benchmark case", exact=True).select_option(
                str(uuid.UUID(int=1000))
            )
            page.get_by_label("Benchmark harness profile", exact=True).select_option(work.HARNESS)
            page.get_by_label("Native matrix repetitions", exact=True).fill("2")
            check = page.get_by_role("button", name="Check entire native matrix", exact=True)
            expect(check).to_be_enabled()
            check.click()
            expect(
                page.get_by_text(
                    "2 runs checked. Cost and duration are unknown until recorded.", exact=True
                )
            ).to_be_visible()
            page.get_by_role("button", name="Create native matrix draft", exact=True).click()
            expect(
                page.get_by_role("heading", name="Browser native matrix", exact=True)
            ).to_be_visible()
            expect(
                page.get_by_text("Start the draft to create its matrix cells.", exact=True)
            ).to_be_visible()
            page.get_by_role("button", name="Start native matrix", exact=True).click()
            expect(
                page.get_by_text("Fixture Pi · native-case · repeat 1 · running", exact=True)
            ).to_be_visible()
            page.get_by_text("Fixture Pi · native-case · repeat 1 · running", exact=True).click()
            expect(page.get_by_role("region", name="Benchmark trial", exact=True)).to_be_visible()
            expect(page.get_by_text("Known run cost: unknown", exact=True)).to_be_visible()
            page.get_by_text("Configurations and history", exact=True).click()
            expect(page.get_by_role("button", name="Add configuration", exact=True)).to_be_disabled()
            page.get_by_role("button", name="Pause new runs", exact=True).click()
            page.get_by_label("Add native matrix profile", exact=True).select_option(work.HARNESS)
            page.get_by_role("button", name="Add configuration", exact=True).click()
            page.get_by_role("button", name="Retire Added Pi", exact=True).click()
            expect(page.get_by_label("Include retired execution history", exact=True)).to_be_visible()
            page.get_by_label("Include retired execution history", exact=True).check()
            page.get_by_role("button", name="Resume native matrix", exact=True).click()
            page.get_by_role("button", name="Cancel native matrix", exact=True).click()
            expect(
                page.get_by_text("Native benchmark matrix · cancelled", exact=True)
            ).to_be_visible()
            assert fixture.controls == ["run", "pause", "resume", "cancel"]
            assert len(fixture.created) == 1 and not errors, errors
            fixture.experiment["status"] = "failed"
            fixture.rows[0]["status"] = "failed"
            page.reload()
            page.get_by_role("button", name="Retry failed cells", exact=True).click()
            expect(
                page.get_by_text("Native benchmark matrix · running", exact=True)
            ).to_be_visible()
            page.get_by_text("Fixture Pi · native-case · repeat 1 · pending", exact=True).click()
            expect(
                page.get_by_role("link", name="Attempt 1 · failed", exact=True)
            ).to_have_attribute("href", "/tasks?task=" + work.TASK)
            expect(
                page.get_by_text("Native correctness: unknown · cost: unknown", exact=True)
            ).to_be_visible()
            page.get_by_text("Clone matrix", exact=True).click()
            page.get_by_label("Native clone name", exact=True).fill("Recorded copy")
            page.get_by_role("button", name="Create matrix copy", exact=True).click()
            expect(page.get_by_role("heading", name="Recorded copy", exact=True)).to_be_visible()
            expect(page.get_by_role("link", name="matrix revision 2", exact=True)).to_have_attribute("href", "/experiments/" + EXPERIMENT)
            assert fixture.changes == [
                ("add", {"harness_profile_id": work.HARNESS}), ("retire", "cfg-02"),
                ("clone", {"name": "Recorded copy", "changes": {"n_runs_per_cell": 2}}),
            ], fixture.changes
            fixture.experiment.update(
                dataset={"source": "benchmark_suite", "suite": "toolathlon"},
                dataset_cases=[],
                status="completed",
                legacy_execution_retired=True,
                n_toolathlon_lanes=3,
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/experiments/{EXPERIMENT}")
            expect(page.get_by_role("status")).to_contain_text(
                "Historical benchmark execution is retired"
            )
            expect(page.get_by_role("button", name="Clone…", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="Re-run", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="CSV", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="JSON", exact=True)).to_be_visible()
            assert (
                fixture.controls == ["run", "pause", "resume", "cancel", "retry-failed"]
                and not errors
            ), errors
            owner.close()
            browser.close()
        print(
            "Native matrix browser acceptance passed: SDK selection, frozen draft, controls, failed-cell retry with saved history and unknown cost, native results and historical exports."
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

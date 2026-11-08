"""Exercise queued native runs, reconnection, outcomes and files in an isolated browser."""

import importlib.util
import os
import threading
import uuid
from copy import deepcopy
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

spec = importlib.util.spec_from_file_location(
    "benchmark_settings", Path(__file__).with_name("benchmark-settings.py")
)
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)
WORKSPACE, PROFILE, DATASET, HARNESS = (
    settings.WORKSPACE,
    settings.PROFILE,
    settings.DATASET,
    settings.HARNESS,
)
TASK, TRIAL, ATTEMPT, ARTIFACT = [str(uuid.UUID(int=i)) for i in range(10, 14)]
RETRY_TASK, RETRY_TRIAL = str(uuid.UUID(int=40)), str(uuid.UUID(int=41))


class StaticFiles(settings.StaticFiles):
    def do_GET(self):
        if self.path == "/tasks":
            self.path = "/index.html"
        super().do_GET()


class Fixture(settings.Fixture):
    def __init__(self):
        super().__init__()
        self.tasks, self.requests, self.cancelled = [], [], 0
        self.compatible, self.first_reply_lost = False, True
        self.evaluation_jobs, self.regrade_requests, self.regrade_results = [], [], []
        self.regrade_reply_lost, self.regrade_cancelled = False, 0
        self.retry_requests, self.retry_trial, self.retry_reply_lost = [], None, True
        self.trial = {
            "id": TRIAL,
            "task_id": TASK,
            "profile_id": PROFILE,
            "dataset_case_id": str(uuid.UUID(int=1000)),
            "snapshot": {},
            "phase": "driving",
            "execution_status": "running",
            "evaluation_status": "pending",
            "recording_status": "open",
            "cleanup_status": "pending",
            "cancel_requested_at": None,
            "created_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
        }

    def finish(self):
        self.trial.update(
            phase="finished",
            execution_status="success",
            evaluation_status="completed",
            recording_status="complete",
            cleanup_status="complete",
            finished_at="2026-01-01T00:01:00Z",
        )
        self.tasks[0]["status"] = "done"

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path, method = parsed.path[4:], request.method
        if not parsed.path.startswith("/api/"):
            return super().route(route)
        assert request.headers.get("x-workspace-id") == WORKSPACE
        body = request.post_data_json
        result = None
        if path == "/benchmarks/preflight":
            result = {
                **settings.AVAILABILITY,
                "compatible": self.compatible,
                "can_run": False,
                "stage": "static",
                "runtime_admission": "pending",
                "missing": [],
                "reasons": []
                if self.compatible
                else [{"code": "missing_capability", "capability": "harness.fixture_feature"}],
            }
        elif path == "/tasks":
            assert method == "GET", "Native creation must use the benchmark queue"
            result = self.tasks
        elif path == "/tasks/" + TASK:
            result = self.tasks[0]
        elif path == "/tasks/" + RETRY_TASK:
            result = self.tasks[1]
        elif path == "/tasks/" + RETRY_TASK + "/attempts":
            result = []
        elif path == "/tasks/" + TASK + "/attempts":
            result = [
                {
                    "id": ATTEMPT,
                    "number": 1,
                    "connector_id": "pi",
                    "connector_version": "0.2.1",
                    "status": "running",
                    "capabilities": {},
                    "commands": {},
                    "completeness": {},
                    "result": None,
                }
            ]
        elif path == "/attempts/" + ATTEMPT + "/events":
            after = int(parse_qs(parsed.query).get("after_seq", ["-1"])[0])
            result = (
                [
                    {
                        "event_id": "fixture-event",
                        "seq": 0,
                        "event": "message.delta",
                        "data": {"text": "Native agent output"},
                    }
                ]
                if after < 0
                else []
            )
        elif path == "/events":
            result = []
        elif path == "/benchmark-trials" and method == "POST":
            self.requests.append(deepcopy(body))
            if not self.tasks:
                self.tasks = [
                    {
                        "id": TASK,
                        "title": body["title"],
                        "description": "Public native problem",
                        "status": "in_progress",
                        "priority": "medium",
                        "origin": "user",
                        "benchmark_trial_id": TRIAL,
                        "result_files": [],
                        "token_usage": {},
                        "retry_count": 0,
                        "max_retries": 0,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]
            if self.first_reply_lost:
                self.first_reply_lost = False
                route.fulfill(status=503, json={"detail": "Fixture reply lost after queue commit"})
                return
            result = {**self.trial, "job_id": str(uuid.UUID(int=14)), "job_status": "queued"}
        elif path == "/benchmark-trials":
            result = (
                [self.retry_trial]
                if parse_qs(parsed.query).get("task_id") == [RETRY_TASK]
                else [self.trial]
            )
        elif path == f"/benchmark-trials/{TRIAL}/retry":
            self.retry_requests.append(deepcopy(body))
            if self.retry_trial is None:
                self.tasks.append(
                    {
                        **self.tasks[0],
                        "id": RETRY_TASK,
                        "status": "ready",
                        "benchmark_trial_id": RETRY_TRIAL,
                        "replay_of_task_id": TASK,
                    }
                )
                self.retry_trial = {
                    **self.trial,
                    "id": RETRY_TRIAL,
                    "task_id": RETRY_TASK,
                    "retry_of_id": TRIAL,
                    "phase": "created",
                    "execution_status": "pending",
                    "evaluation_status": "pending",
                    "cleanup_status": "pending",
                    "recording_status": "open",
                    "cancel_requested_at": None,
                    "finished_at": None,
                }
            if self.retry_reply_lost:
                self.retry_reply_lost = False
                route.fulfill(status=503, json={"detail": "Synthetic lost retry response"})
                return
            result = self.retry_trial
        elif path == f"/benchmark-trials/{RETRY_TRIAL}":
            result = self.retry_trial
        elif path == f"/benchmark-trials/{RETRY_TRIAL}/events":
            result = {"events": [], "next_seq": -1}
        elif path in {
            f"/benchmark-trials/{RETRY_TRIAL}/evaluations",
            f"/benchmark-trials/{RETRY_TRIAL}/artifacts",
            f"/benchmark-trials/{RETRY_TRIAL}/evaluation-jobs",
        }:
            result = []
        elif path == "/benchmark-trials/" + TRIAL:
            result = self.trial
        elif path == f"/benchmark-trials/{TRIAL}/evaluation-jobs":
            result = self.evaluation_jobs
        elif path == f"/benchmark-trials/{TRIAL}/regrade":
            self.regrade_requests.append(deepcopy(body))
            job = next(
                (row for row in self.evaluation_jobs if row["request_id"] == body["request_id"]),
                None,
            )
            if job is None:
                job = {
                    "id": str(uuid.UUID(int=20 + len(self.evaluation_jobs))),
                    "request_id": body["request_id"],
                    "trial_id": TRIAL,
                    "source_evaluation_id": body["source_evaluation_id"],
                    "status": "queued",
                    "phase": "queued",
                    "error_code": None,
                    "cancel_requested_at": None,
                    "evaluation_id": None,
                    "recording_status": None,
                    "cleanup_status": None,
                }
                self.evaluation_jobs.append(job)
            if self.regrade_reply_lost:
                self.regrade_reply_lost = False
                route.fulfill(status=503, json={"detail": "Synthetic lost regrade response"})
                return
            result = job
        elif path.startswith(f"/benchmark-trials/{TRIAL}/evaluation-jobs/") and path.endswith(
            "/cancel"
        ):
            job = next(row for row in self.evaluation_jobs if row["id"] == path.split("/")[-2])
            job["cancel_requested_at"] = "2026-01-01T00:02:00Z"
            self.regrade_cancelled += 1
            result = {"requested": True}
        elif path.endswith("/cancel") and path.startswith("/benchmark-trials/"):
            self.cancelled += 1
            self.trial["cancel_requested_at"] = "2026-01-01T00:00:30Z"
            result = {"requested": True}
        elif path == f"/benchmark-trials/{TRIAL}/evaluations":
            result = (
                []
                if self.trial["evaluation_status"] != "completed"
                else [
                    {
                        "id": str(uuid.UUID(int=15)),
                        "kind": "initial",
                        "status": "completed",
                        "verdict": "incorrect",
                        "success": False,
                        "error_kind": None,
                        "metrics": [{"name": "native.reward", "value": 0, "aggregation": "native"}],
                    }
                ]
            )
            result += self.regrade_results
        elif path == f"/benchmark-trials/{TRIAL}/artifacts":
            result = [
                {
                    "id": ARTIFACT,
                    "artifact": {
                        "id": "native-output.tar",
                        "visibility": "user",
                        "size_bytes": 18,
                        "digest": "sha256:" + "a" * 64,
                        "media_type": "application/x-tar",
                    },
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        elif path == f"/benchmark-trials/{TRIAL}/artifacts/{ARTIFACT}/download":
            route.fulfill(body=b"fixture-file-bytes", content_type="application/octet-stream")
            return
        elif path == f"/benchmark-trials/{TRIAL}/events":
            after = int(parse_qs(parsed.query).get("after_seq", ["-1"])[0])
            result = {
                "events": [
                    {
                        "seq": 0,
                        "event": "execution.confirmed",
                        "data": {},
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]
                if after < 0
                else [],
                "next_seq": max(after, 0),
            }
        else:
            assert method == "GET", "Unexpected mutation: " + path
            return super().route(route)
        route.fulfill(json=deepcopy(result))


def main():
    dist = Path(os.environ["EVALHIVE_FRONTEND_DIST"]).resolve(strict=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticFiles, directory=str(dist)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixture, errors = Fixture(), []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            owner = settings.context(browser, "owner")
            owner.route("**/*", fixture.route)
            page = owner.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/tasks")
            page.get_by_role("button", name="New Task", exact=True).click()
            page.get_by_label("Task type", exact=True).select_option("benchmark")
            page.get_by_label("Benchmark run title", exact=True).fill("Browser native run")
            page.get_by_label("Run benchmark profile", exact=True).select_option(PROFILE)
            page.get_by_label("Run dataset revision", exact=True).select_option(DATASET)
            page.get_by_label("Run benchmark case", exact=True).select_option(
                str(uuid.UUID(int=1000))
            )
            page.get_by_label("Benchmark harness profile", exact=True).select_option(HARNESS)
            launch = page.get_by_role("button", name="Queue benchmark run", exact=True)
            expect(launch).to_be_disabled()
            expect(
                page.get_by_text(
                    "This selection does not provide a required capability: harness.fixture_feature.",
                    exact=True,
                )
            ).to_be_visible()
            fixture.compatible = True
            page.get_by_label("Benchmark harness profile", exact=True).select_option("")
            page.get_by_label("Benchmark harness profile", exact=True).select_option(HARNESS)
            expect(launch).to_be_enabled()
            launch.click()
            expect(page.get_by_role("alert").filter(has_text="reply lost")).to_be_visible()
            launch.click()
            expect(
                page.get_by_role("heading", name="Browser native run", exact=True)
            ).to_be_visible()
            assert len(fixture.requests) == 2 and fixture.requests[0] == fixture.requests[1]
            assert len(fixture.tasks) == 1
            page.get_by_role("heading", name="Browser native run", exact=True).click()
            panel = page.get_by_role("region", name="Benchmark trial", exact=True)
            expect(panel.get_by_text("Native agent output", exact=False)).to_be_visible()
            expect(
                panel.get_by_role("button", name="Cancel benchmark run", exact=True)
            ).to_be_visible()
            expect(page.get_by_role("button", name="Send feedback", exact=True)).to_have_count(0)
            fixture.finish()
            expect(panel.get_by_text("initial · checker: completed", exact=True)).to_be_visible(
                timeout=10000
            )
            expect(
                panel.get_by_text("Task correctness: incorrect · incorrect", exact=True)
            ).to_be_visible()
            expect(panel.get_by_text("native.reward: 0 · native", exact=True)).to_be_visible()
            fixture.regrade_reply_lost = True
            panel.get_by_role("button", name="Regrade saved state", exact=True).click()
            expect(panel.get_by_text("Regrade · queued · queued", exact=True)).to_be_visible()
            expect(
                panel.get_by_role("button", name="Regrade saved state", exact=True)
            ).to_be_disabled()
            expect(panel.get_by_text("Synthetic lost regrade response", exact=True)).to_have_count(
                0
            )
            assert len(fixture.evaluation_jobs) == 1 and len(fixture.regrade_requests) == 1
            first_regrade = fixture.evaluation_jobs[0]
            first_regrade.update(
                status="completed",
                phase="finished",
                cleanup_status="complete",
                recording_status="complete",
                evaluation_id=first_regrade["id"],
            )
            fixture.regrade_results = [
                {
                    "id": first_regrade["id"],
                    "kind": "regrade",
                    "status": "completed",
                    "verdict": "correct",
                    "success": True,
                    "error_kind": None,
                    "metrics": [{"name": "native.reward", "value": 1, "aggregation": "native"}],
                }
            ]
            expect(panel.get_by_text("regrade · checker: completed", exact=True)).to_be_visible(
                timeout=10000
            )
            expect(panel.get_by_text("initial · checker: completed", exact=True)).to_be_visible()
            expect(
                panel.get_by_text("Task correctness: incorrect · incorrect", exact=True)
            ).to_be_visible()
            expect(
                panel.get_by_text("Task correctness: correct · correct", exact=True)
            ).to_be_visible()
            panel.get_by_label("Source native evaluation", exact=True).select_option(
                str(uuid.UUID(int=15))
            )
            panel.get_by_role("button", name="Regrade saved state", exact=True).click()
            expect(panel.get_by_text("Regrade · queued · queued", exact=True)).to_be_visible()
            second_regrade = fixture.evaluation_jobs[1]
            panel.get_by_role(
                "button", name=f"Cancel regrade {second_regrade['id']}", exact=True
            ).click()
            expect(
                panel.get_by_role(
                    "button", name=f"Cancel regrade {second_regrade['id']}", exact=True
                )
            ).to_be_disabled()
            assert fixture.regrade_cancelled == 1 and fixture.cancelled == 0
            assert (
                fixture.regrade_requests[0]["request_id"]
                != fixture.regrade_requests[1]["request_id"]
            )
            assert (
                fixture.trial["phase"] == "finished"
                and fixture.trial["cancel_requested_at"] is None
            )
            second_regrade.update(status="cancelled", phase="cancelled")
            expect(page.get_by_role("button", name="Evaluate quality", exact=True)).to_have_count(0)
            with page.expect_download() as download:
                panel.get_by_role("button", name="native-output.tar", exact=False).click()
            assert Path(download.value.path()).read_bytes() == b"fixture-file-bytes"
            page.reload()
            page.get_by_role("heading", name="Browser native run", exact=True).click()
            expect(page.get_by_text("initial · checker: completed", exact=True)).to_be_visible()
            fixture.trial.update(
                phase="driving",
                execution_status="running",
                evaluation_status="pending",
                finished_at=None,
            )
            page.reload()
            page.get_by_role("heading", name="Browser native run", exact=True).click()
            page.get_by_role("button", name="Cancel benchmark run", exact=True).click()
            expect(
                page.get_by_role("button", name="Cancellation requested", exact=True)
            ).to_be_disabled()
            assert fixture.cancelled == 1
            fixture.finish()
            expect(
                panel.get_by_role("button", name="Repeat with recorded settings", exact=True)
            ).to_be_visible(timeout=10000)
            panel.get_by_role("button", name="Repeat with recorded settings", exact=True).click()
            expect(panel.get_by_text("Synthetic lost retry response", exact=True)).to_be_visible()
            panel.get_by_role("button", name="Repeat with recorded settings", exact=True).click()
            expect(page).to_have_url(
                f"http://127.0.0.1:{server.server_port}/tasks?task={RETRY_TASK}"
            )
            expect(page.get_by_role("link", name="Previous execution", exact=True)).to_be_visible()
            assert len(fixture.tasks) == 2 and len(fixture.retry_requests) == 2
            assert fixture.retry_requests[0] == fixture.retry_requests[1]
            page.get_by_role("link", name="Previous execution", exact=True).click()
            expect(page).to_have_url(f"http://127.0.0.1:{server.server_port}/tasks?task={TASK}")
            expect(page.get_by_text("initial · checker: completed", exact=True)).to_be_visible()
            assert not errors, errors
            owner.close()
            viewer = settings.context(browser, "viewer")
            viewer.route("**/*", fixture.route)
            readonly = viewer.new_page()
            readonly.goto(f"http://127.0.0.1:{server.server_port}/tasks")
            readonly.get_by_role("heading", name="Browser native run", exact=True).first.click()
            expect(
                readonly.get_by_role("button", name="Cancel benchmark run", exact=True)
            ).to_have_count(0)
            expect(
                readonly.get_by_role("button", name="Regrade saved state", exact=True)
            ).to_have_count(0)
            expect(
                readonly.get_by_role("button", name="Repeat with recorded settings", exact=True)
            ).to_have_count(0)
            viewer.close()
            browser.close()
        print(
            "Benchmark Work browser acceptance passed: queued runs, output/files, independent regrade, frozen repeat with lost-response retry, previous-execution links, preserved history, cancellation and read-only role."
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

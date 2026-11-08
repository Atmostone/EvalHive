"""Exercise benchmark settings against a local production build and an isolated API fixture.

Run with Python Playwright in an engineering browser image. Set
EVALHIVE_FRONTEND_DIST to the read-only frontend build directory.
"""

import json
import os
import threading
import uuid
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

WORKSPACE, PROFILE, DATASET, HARNESS = [str(uuid.UUID(int=i)) for i in range(1, 5)]
AVAILABILITY = {"installed": True, "configured": True, "compatible": None, "live_verified": False}
MANIFEST = {
    "id": "fixture.benchmark",
    "version": "1.0.0",
    "display_name": "External fixture benchmark",
    "protocol_version": "1.0",
    "requirements": {"capabilities": ["model.user"]},
    "config_schema": {
        "type": "object",
        "properties": {"steps": {"title": "Step limit", "type": "integer", "minimum": 1}},
        "required": ["steps"],
        "additionalProperties": False,
    },
    "secret_schema": {
        "type": "object",
        "properties": {"service": {"type": "string"}},
        "additionalProperties": False,
    },
    "capabilities": {
        "prepare": "native",
        "drive": "native",
        "cleanup": "native",
        "regrade": "unsupported",
    },
}


class StaticFiles(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/settings":
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *args):
        pass


class Fixture:
    def __init__(self):
        self.profiles = [
            {
                "id": PROFILE,
                "name": "Existing benchmark",
                "connector_id": MANIFEST["id"],
                "connector_version": "1.0.0",
                "config": {"steps": 2},
                "secret_refs": {},
                "model_bindings": {},
                **AVAILABILITY,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ]
        (
            self.credentials,
            self.created_profiles,
            self.credential_writes,
            self.registrations,
            self.preflights,
        ) = [], [], [], [], []
        self.datasets = [
            {
                "id": DATASET,
                "connector_id": MANIFEST["id"],
                "connector_version": "1.0.0",
                "dataset_id": "fixture-set",
                "revision": "original-revision",
                "source_digest": "sha256:" + "a" * 64,
                "receipt_digest": "sha256:" + "b" * 64,
                "worker_image": "sha256:" + "c" * 64,
                "case_count": 201,
                "created_at": "2026-01-01T00:00:00Z",
                "live_verified": False,
            }
        ]

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if not parsed.path.startswith("/api/"):
            if parsed.hostname != "127.0.0.1":
                route.abort()
            else:
                route.continue_()
            return
        assert request.headers.get("x-workspace-id") == WORKSPACE
        path, method, body = parsed.path[4:], request.method, request.post_data_json
        result = None
        if path == "/benchmarks":
            result = [{"manifest": MANIFEST, **AVAILABILITY, "configured": None}]
        elif path == "/benchmark-profiles":
            if method == "POST":
                self.created_profiles.append(deepcopy(body))
                self.profiles.append({**body, "id": str(uuid.uuid4()), **AVAILABILITY})
                result = self.profiles[-1]
            else:
                result = self.profiles
        elif path == "/benchmark-secrets":
            if method == "POST":
                self.credential_writes.append(deepcopy(body))
                self.credentials.append(
                    {"id": str(uuid.uuid4()), "name": body["name"], "configured": True}
                )
                result = self.credentials[-1]
            else:
                result = self.credentials
        elif path == "/benchmark-datasets":
            if method == "POST":
                self.registrations.append(deepcopy(body))
                result = {
                    **self.datasets[0],
                    "id": str(uuid.uuid4()),
                    "dataset_id": body["dataset_id"],
                    "revision": body["revision"],
                }
                self.datasets.append(result)
            else:
                result = self.datasets
        elif path.startswith("/benchmark-datasets/") and path.endswith("/cases"):
            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
            result = {
                "total": 201,
                "offset": offset,
                "cases": [
                    {
                        "id": str(uuid.UUID(int=1000 + i)),
                        "title": f"Fixture case {i}",
                        "description": f"Public problem {i}",
                        "ref": {
                            "dataset_id": "fixture-set",
                            "revision": "original-revision",
                            "case_id": f"original-case-{i}",
                        },
                        "source_digest": "sha256:" + "d" * 64,
                        "requirements": {"capabilities": []},
                    }
                    for i in range(offset, min(offset + 100, 201))
                ],
            }
        elif path == "/benchmarks/preflight":
            self.preflights.append(deepcopy(body))
            result = {
                **AVAILABILITY,
                "compatible": False,
                "can_run": False,
                "stage": "static",
                "runtime_admission": "pending",
                "reasons": [
                    {"code": "missing_capability", "capability": "harness.fixture_feature"}
                ],
                "missing": [],
            }
        elif path == "/harness-profiles":
            result = [
                {
                    "id": HARNESS,
                    "name": "Fixture Pi",
                    "connector_id": "pi",
                    "connector_version": "0.2.1",
                    "config": {},
                    "secret_refs": {},
                    "is_default": True,
                }
            ]
        elif path in (
            "/harnesses",
            "/harness-secrets",
            "/providers",
            "/registry",
            "/registry/tools",
        ):
            result = []
        elif path == "/health":
            result = {"services": {}, "version": "browser-fixture"}
        elif path == "/workspaces/me/system-models":
            result = {}
        elif path == "/settings":
            result = {}
        else:
            route.fulfill(status=404, json={"detail": "Fixture route not configured"})
            return
        route.fulfill(json=deepcopy(result))


def context(browser, role):
    value = {
        "state": {
            "token": "synthetic-browser-session",
            "workspaceId": WORKSPACE,
            "user": {"id": WORKSPACE, "email": "fixture@example.invalid"},
            "workspaces": [{"id": WORKSPACE, "name": "Fixture", "slug": "fixture", "role": role}],
        },
        "version": 0,
    }
    selected = browser.new_context()
    selected.add_init_script(
        "localStorage.setItem('evalhive.auth', " + json.dumps(json.dumps(value)) + ")"
    )
    return selected


def main():
    dist = Path(os.environ["EVALHIVE_FRONTEND_DIST"]).resolve(strict=True)
    assert (dist / "index.html").is_file()
    fixture = Fixture()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticFiles, directory=str(dist)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/settings"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            owner = context(browser, "owner")
            owner.route("**/*", fixture.route)
            page = owner.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            profiles = page.locator("section").filter(
                has=page.get_by_role("heading", name="Benchmark profiles", exact=True)
            )
            expect(profiles.get_by_text("Existing benchmark", exact=True)).to_be_visible()
            expect(profiles.get_by_text("Live verified: not verified", exact=True)).to_be_visible()
            page.get_by_role("button", name="New benchmark profile", exact=True).click()
            page.get_by_label("Benchmark profile name", exact=True).fill("Browser-created profile")
            page.get_by_label("Step limit", exact=True).fill("0")
            expect(
                page.get_by_role("button", name="Save benchmark profile", exact=True)
            ).to_be_disabled()
            page.get_by_label("Step limit", exact=True).fill("3")
            page.get_by_role("button", name="Save benchmark profile", exact=True).click()
            expect(profiles.get_by_text("Browser-created profile", exact=True)).to_be_visible()
            assert fixture.created_profiles[0]["config"] == {"steps": 3}
            assert fixture.created_profiles[0]["connector_id"] == MANIFEST["id"]
            page.get_by_text("Stored benchmark credentials", exact=True).click()
            page.get_by_label("Benchmark credential name", exact=True).fill(
                "Browser-only credential"
            )
            secret = page.get_by_label("New benchmark credential value", exact=True)
            expect(secret).to_have_attribute("type", "password")
            secret.fill("synthetic-write-only-value")
            page.get_by_role("button", name="Store benchmark credential", exact=True).click()
            expect(secret).to_have_value("")
            assert fixture.credential_writes == [
                {"name": "Browser-only credential", "value": "synthetic-write-only-value"}
            ]
            assert "synthetic-write-only-value" not in page.locator("body").inner_text()
            page.get_by_text("Register installed dataset revision", exact=True).click()
            page.get_by_label("Dataset benchmark profile", exact=True).select_option(PROFILE)
            page.get_by_label("Installed dataset ID", exact=True).fill("installed-fixture")
            page.get_by_label("Installed dataset revision", exact=True).fill("installed-revision")
            page.get_by_role("button", name="Register revision", exact=True).click()
            expect(page.get_by_text("Dataset revision registered.", exact=True)).to_be_visible()
            assert fixture.registrations == [
                {
                    "profile_id": PROFILE,
                    "dataset_id": "installed-fixture",
                    "revision": "installed-revision",
                }
            ]
            page.get_by_role("button", name="Browse cases", exact=True).first.click()
            expect(page.get_by_text("Fixture case 0", exact=True)).to_be_visible()
            page.get_by_role("button", name="Next cases", exact=True).click()
            expect(page.get_by_text("Fixture case 100", exact=True)).to_be_visible()
            page.get_by_label("Run benchmark profile", exact=True).select_option(PROFILE)
            page.get_by_label("Run dataset revision", exact=True).select_option(DATASET)
            case = str(uuid.UUID(int=1000))
            expect(page.get_by_label("Run benchmark case", exact=True)).to_be_enabled()
            page.get_by_label("Run benchmark case", exact=True).select_option(case)
            page.get_by_label("Benchmark harness profile", exact=True).select_option(HARNESS)
            expect(
                page.get_by_text(
                    "This selection does not provide a required capability: harness.fixture_feature.",
                    exact=True,
                )
            ).to_be_visible()
            assert fixture.preflights[-1] == {
                "profile_id": PROFILE,
                "dataset_revision_id": DATASET,
                "case_id": case,
                "harness_profile_id": HARNESS,
            }
            expect(page.get_by_text("Live verified: yes", exact=True)).to_have_count(0)
            assert not errors, errors
            owner.close()
            viewer = context(browser, "viewer")
            viewer.route("**/*", fixture.route)
            readonly = viewer.new_page()
            readonly.goto(url)
            readonly_profiles = readonly.locator("section").filter(
                has=readonly.get_by_role("heading", name="Benchmark profiles", exact=True)
            )
            expect(readonly_profiles.get_by_text("Existing benchmark", exact=True)).to_be_visible()
            expect(
                readonly.get_by_role("button", name="New benchmark profile", exact=True)
            ).to_have_count(0)
            expect(
                readonly.get_by_role("button", name="Store benchmark credential", exact=True)
            ).to_have_count(0)
            expect(
                readonly.get_by_text("Register installed dataset revision", exact=True)
            ).to_have_count(0)
            viewer.close()
            browser.close()
        print(
            "Benchmark settings browser acceptance passed: schema validation, profiles, write-only credentials, dataset pagination, preflight and read-only roles."
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

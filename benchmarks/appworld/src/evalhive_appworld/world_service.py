"""Persist remote native effects before the independent native evaluator reads them."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from evalhive_benchmark.world_control import serve
from evalhive_benchmark.world_rpc import WorldError

from .api_process import ApiProcess
from .dataset import InstalledAppWorld
from .groups import native_groups
from .native import canonical, load


class NativeService:
    def __init__(
        self, installed, *, root="/appworld", records="/records", connection="/connection"
    ):
        self.installed, self.root = installed, Path(root)
        self.records, self.connection = Path(records), Path(connection)
        self.api, self.world = ApiProcess(self.records), None
        self.ready = self.closed = self.saved = False
        self.captured = self.verdict = None

    def __call__(self, method, payload):
        if method == "cleanup":
            if payload:
                raise WorldError("Native cleanup does not accept parameters")
            return self.cleanup()
        if self.closed:
            raise WorldError("Native world is closed")
        if method == "prepare":
            return self.prepare(payload)
        if not self.ready:
            raise WorldError("Native world has not been prepared")
        if method == "capture":
            return self.capture(payload)
        if method == "verify" and not payload:
            return self.verify()
        raise WorldError("Native world operation is unavailable")

    def prepare(self, payload):
        if self.world is not None or set(payload) != {"case"}:
            raise WorldError("Native preparation is frozen")
        task = self.installed.resolve(payload["case"])
        native = load(self.root)
        self.api.start()
        self.world = native.AppWorld(
            task_id=task.id,
            experiment_name="evalhive",
            remote_apis_url="http://127.0.0.1:9000",
            load_ground_truth=True,
        )
        if self.world.models is not None:
            raise WorldError("Native evaluation requires an independent API process")
        groups = native_groups()
        self.connection.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.connection / "connection.json").open("xb") as stream:
            stream.write(
                canonical(
                    {
                        "format_version": "1.0",
                        "service": "appworld",
                        "source_digest": self.installed.receipt["source_digest"],
                        "api_url": "http://appworld:8080",
                        "groups": groups,
                    }
                )
            )
        self.api.control("open")
        self.ready = True
        # Supervisor identity is public native task context; ground truth stays private.
        return {
            "prompt": task.instruction,
            "supervisor": dict(task.supervisor),
            "groups": groups,
        }

    def persist(self):
        if self.world is None or self.saved:
            return
        self.api.control("seal")
        self.world.save()
        self.saved = True
        proof = {"saved": True, "api_process_separate": True, "public_api_sealed": True}
        self.records.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.records / "persistence.json").write_bytes(canonical(proof))

    def capture(self, payload):
        if set(payload) != {"agent_success"} or type(payload["agent_success"]) is not bool:
            raise WorldError("Native capture requires the agent completion receipt")
        if self.captured is not None:
            if self.captured["agent_success"] != payload["agent_success"]:
                raise WorldError("Native capture conflicts with its confirmed receipt")
            return self.captured
        self.persist()
        self.captured = {"saved": True, **payload}
        return self.captured

    def verify(self):
        if self.captured is None or not self.saved:
            raise WorldError("Native evaluation requires persisted API state")
        if self.verdict is not None:
            return self.verdict
        tracker = self.world.evaluate(suppress_errors=True)
        native = tracker.to_dict()
        report = self.records / "report"
        report.mkdir(mode=0o700, parents=True, exist_ok=True)
        (report / "test-tracker.json").write_bytes(canonical(native))
        self.verdict = {
            "success": tracker.success,
            "native_verdict_complete": True,
            "task_id": self.world.task_id,
            "scenario_id": self.world.task.generator_id,
            "tracker_sha256": hashlib.sha256(canonical(native)).hexdigest(),
            "saved_before_evaluation": True,
        }
        (report / "verdict.json").write_bytes(canonical(self.verdict))
        return self.verdict

    def cleanup(self):
        if self.closed:
            return {"cleaned": True}
        # A failed save deliberately prevents a successful cleanup receipt.
        try:
            self.persist()
            if self.world is not None:
                self.world.close()
        finally:
            self.api.close()
        self.closed = True
        return {"cleaned": True}


def main():
    sys.stdout = sys.stderr
    root = Path("/dataset")
    path = root / "installation.json"
    if path.is_symlink() or path.stat().st_size > 32 * 1024**2:
        raise WorldError("Native installation metadata is unavailable")

    def factory(effect):
        return NativeService(
            InstalledAppWorld(Path(os.environ["APPWORLD_ROOT"]), json.loads(path.read_text()))
        )

    serve(factory)


if __name__ == "__main__":
    main()

"""Own private evaluation inputs and fresh native grading outside the agent container."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import sys
import uuid
from pathlib import Path

from evalhive_benchmark.world_control import serve
from evalhive_benchmark.world_rpc import WorldError

from .dataset import InstalledSWEbench
from .evaluation import MAX_PATCH_BYTES, grade
from .native import canonical
from .task_transfer import regular
from .verification import apply_patch, native_spec


class NativeService:
    def __init__(
        self,
        installed,
        effect,
        *,
        stage="/stage",
        records="/records",
        checkpoint="/checkpoint",
        captured="/evalhive-capture",
        logs="/evalhive-results",
        bindings=None,
    ):
        self.installed, self.effect = installed, effect
        self.stage, self.records, self.checkpoint = Path(stage), Path(records), Path(checkpoint)
        self.capture_root, self.logs = Path(captured), Path(logs)
        self.bindings, self.image_commit = bindings, None
        self.case = self.instance = self.test_spec = self.patch = self.captured = self.verdict = (
            None
        )
        self.closed = False

    def __call__(self, method, payload):
        if method == "cleanup" and not payload:
            self.closed = True
            return {"cleaned": True}
        if self.closed:
            raise WorldError("Native evaluation service is closed")
        if method == "prepare":
            return self.prepare(payload)
        if self.test_spec is None:
            raise WorldError("Native evaluation has not been prepared")
        if method == "capture" and not payload:
            return self.capture()
        if method == "verify" and not payload:
            return self.verify()
        raise WorldError("Native evaluation operation is unavailable")

    def execute(self, resource, argv, *, cwd="/testbed"):
        result = self.effect("exec", {"resource": resource, "argv": argv, "cwd": cwd})
        if (
            not isinstance(result, dict)
            or type(result.get("exit_code")) is not int
            or result.get("stdout_truncated")
            or result.get("stderr_truncated")
        ):
            raise WorldError("Native process evidence is incomplete")
        return result

    def transfer(self, operation, payload):
        encoded = base64.b64encode(canonical(payload)).decode()
        result = self.execute(
            "task", ["/usr/bin/python3", "/evalhive-tools/task_transfer.py", operation, encoded]
        )
        if result["exit_code"] != 0:
            raise WorldError("Native patch capture failed")
        return json.loads(result["stdout"])

    def prepare(self, payload):
        if (
            self.test_spec is not None
            or set(payload) - {"case", "checkpoint"}
            or "case" not in payload
        ):
            raise WorldError("Native evaluation inputs are frozen")
        self.case = payload["case"]
        self.instance = self.installed.resolve(self.case)
        binding = self.bindings["cases"][self.case["ref"]["case_id"]]
        if binding["source_digest"] != self.case["source_digest"] or not re.fullmatch(
            r"[a-f0-9]{40}", binding["image_commit"]
        ):
            raise WorldError("Native image baseline differs from its installation")
        self.image_commit = binding["image_commit"]
        self.test_spec = native_spec(self.instance)
        for path in (self.stage, self.records, self.checkpoint):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.stage / "eval.sh").open("xb") as stream:
            stream.write(self.test_spec.eval_script.encode())
        if "checkpoint" in payload:
            saved = payload["checkpoint"]
            identity = saved["artifact_id"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", identity):
                raise WorldError("Native checkpoint artifact identity is invalid")
            self.restore(Path("/evalhive/retained") / identity / "payload", saved)
        else:
            result = self.execute("task", ["git", "rev-parse", "HEAD"])
            if result["exit_code"] != 0 or result["stdout"].strip() != self.image_commit:
                raise WorldError("Native task image does not contain the frozen image baseline")
            ancestor = self.execute(
                "task",
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    self.instance["base_commit"],
                    self.image_commit,
                ],
            )
            clean = self.execute("task", ["git", "status", "--porcelain", "--untracked-files=no"])
            if ancestor["exit_code"] != 0 or clean["exit_code"] != 0 or clean["stdout"].strip():
                raise WorldError(
                    "Native task image has inconsistent source ancestry or tracked changes"
                )
        return {"prepared": True, "source_digest": self.case["source_digest"]}

    def save_patch(self, data):
        if len(data) > MAX_PATCH_BYTES:
            raise WorldError("Native patch exceeds its byte budget")
        self.patch = data.decode("utf-8")
        proof = {
            "source_digest": self.case["source_digest"],
            "base_commit": self.instance["base_commit"],
            "image_commit": self.image_commit,
            "patch_sha256": hashlib.sha256(data).hexdigest(),
            "patch_size_bytes": len(data),
        }
        for path in (self.stage / "patch.diff", self.checkpoint / "patch.diff"):
            with path.open("xb") as stream:
                stream.write(data)
        with (self.checkpoint / "patch.json").open("xb") as stream:
            stream.write(canonical(proof))
        self.captured = proof
        return proof

    def capture(self):
        if self.captured is not None:
            return self.captured
        identity = uuid.uuid4().hex
        receipt = self.transfer("capture", {"id": identity, "base_commit": self.image_commit})
        if (
            set(receipt) != {"id", "base_commit", "size_bytes", "sha256"}
            or receipt["id"] != identity
            or receipt["base_commit"] != self.image_commit
            or type(receipt["size_bytes"]) is not int
            or not 0 <= receipt["size_bytes"] <= MAX_PATCH_BYTES
            or not re.fullmatch(r"[a-f0-9]{64}", receipt["sha256"])
        ):
            raise WorldError("Native patch capture receipt is invalid")
        data = regular(self.capture_root / identity / "patch.diff")
        if (
            len(data) != receipt["size_bytes"]
            or hashlib.sha256(data).hexdigest() != receipt["sha256"]
        ):
            raise WorldError("Native patch differs from its confirmed receipt")
        return self.save_patch(bytes(data))

    def restore(self, archive, saved):
        import tarfile

        data = regular(archive, maximum=MAX_PATCH_BYTES + 128 * 1024)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as stream:
            members = stream.getmembers()
            files = {row.name: row for row in members if row.isfile()}
            if (
                set(files) != {"checkpoint/patch.diff", "checkpoint/patch.json"}
                or len(members) > 3
                or len(files) != sum(row.isfile() for row in members)
                or any(
                    not row.isfile() and not (row.isdir() and row.name == "checkpoint")
                    for row in members
                )
                or files["checkpoint/patch.diff"].size > MAX_PATCH_BYTES
                or files["checkpoint/patch.json"].size > 4096
            ):
                raise WorldError("Native patch checkpoint has invalid contents")
            metadata = json.load(stream.extractfile(files["checkpoint/patch.json"]))
            data = stream.extractfile(files["checkpoint/patch.diff"]).read(MAX_PATCH_BYTES + 1)
        if metadata != {
            key: saved[key]
            for key in (
                "source_digest",
                "base_commit",
                "image_commit",
                "patch_sha256",
                "patch_size_bytes",
            )
        }:
            raise WorldError("Native checkpoint metadata differs from its frozen state")
        expected = {
            "source_digest": self.case["source_digest"],
            "base_commit": self.instance["base_commit"],
            "image_commit": self.image_commit,
            "patch_sha256": hashlib.sha256(data).hexdigest(),
            "patch_size_bytes": len(data),
        }
        if metadata != expected:
            raise WorldError("Native checkpoint patch differs from this task")
        self.save_patch(data)

    def verify(self):
        if self.captured is None:
            raise WorldError("Native evaluation requires a captured patch")
        if self.verdict is not None:
            return self.verdict
        baseline = self.execute("evaluator", ["git", "rev-parse", "HEAD"])
        if baseline["exit_code"] != 0 or baseline["stdout"].strip() != self.image_commit:
            raise WorldError("Fresh evaluator differs from the frozen image baseline")
        prepared = self.execute(
            "evaluator",
            [
                "/usr/bin/python3",
                "/evalhive-tools/eval_transfer.py",
                "prepare",
                self.captured["patch_sha256"],
            ],
        )
        if prepared["exit_code"] != 0:
            raise WorldError("Native evaluator could not stage its patch")

        async def execute(argv):
            return self.execute("evaluator", argv)

        applied = asyncio.run(apply_patch(execute))
        with (self.records / "application.json").open("xb") as stream:
            stream.write(canonical(applied))
        if applied["applied"]:
            tested = self.execute(
                "evaluator", ["/usr/bin/python3", "/evalhive-tools/eval_transfer.py", "test"]
            )
            if tested["exit_code"] != 0:
                raise WorldError("Native evaluator did not record complete test evidence")
            log = self.read_log()
        else:
            from swebench.harness.constants import APPLY_PATCH_FAIL

            log = (APPLY_PATCH_FAIL + "\n").encode()
        path = self.records / "test_output.txt"
        with path.open("xb") as stream:
            stream.write(log)
        result = grade(self.test_spec, self.patch, path, self.records / "runs")
        report = result["report"][self.test_spec.instance_id]
        self.verdict = {
            "success": report["resolved"],
            "native_verdict_complete": True,
            "native": result,
        }
        return self.verdict

    def read_log(self):
        proof = json.loads(regular(self.logs / "receipt.json", maximum=4096))
        from .evaluation import MAX_LOG_BYTES

        if (
            type(proof.get("size_bytes")) is not int
            or not 0 <= proof["size_bytes"] <= MAX_LOG_BYTES
        ):
            raise WorldError("Native test log exceeds its byte budget")
        data = regular(self.logs / "test_output.txt", maximum=MAX_LOG_BYTES)
        if len(data) != proof["size_bytes"] or hashlib.sha256(data).hexdigest() != proof.get(
            "sha256"
        ):
            raise WorldError("Native test evidence differs from its receipt")
        return bytes(data)


def main():
    sys.stdout = sys.stderr
    root = Path("/dataset")
    receipt = json.loads(regular(root / "installation.json", maximum=64 * 1024**2))
    bindings = json.loads(regular(root / "runtime.json", maximum=64 * 1024**2))
    serve(
        lambda effect: NativeService(
            InstalledSWEbench(root / "native", receipt), effect, bindings=bindings
        )
    )


if __name__ == "__main__":
    main()

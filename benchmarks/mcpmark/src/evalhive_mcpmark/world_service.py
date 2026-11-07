"""Persistent native state, private checkers and explicit grading completeness."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .assets import filesystem_asset, postgres_asset
from .dataset import InstalledMCPMark
from .native import NativeConfigurationError, canonical
from .postgres_transport import restore_transport
from .world_credentials import read as read_credentials
from .world_rpc import MAX_BYTES, WorldError, encoded
from .world_stage import read as read_messages


class NativeService:
    def __init__(
        self,
        installed,
        *,
        initial_root="/initial",
        connection_root="/connection",
        records_root="/records",
        effect=None,
    ):
        self.installed = installed
        self.initial_root, self.connection_root, self.records_root = map(
            Path, (initial_root, connection_root, records_root)
        )
        self.task, self.state, self.captured, self.verdict = None, None, None, None
        self.closed, self.ready = False, False
        self.effect = effect
        self.secret_values = ()

    def __call__(self, method, payload):
        if method == "cleanup":
            if payload:
                raise WorldError("Native cleanup does not accept parameters")
            if self.closed:
                return {"cleaned": True}
            if self.state is not None and not self.state.clean_up(self.task):
                raise WorldError("Native cleanup was not confirmed")
            self.closed = True
            return {"cleaned": True}
        if self.closed:
            raise WorldError("Native service is closed")
        if method == "prepare":
            return self.prepare(payload)
        if not self.ready:
            raise WorldError("Native state has not been prepared")
        if method == "capture":
            return self.capture(payload)
        if method == "verify":
            return self.verify(payload)
        raise WorldError("Native service method is unavailable")

    def prepare(self, payload):
        if self.task is not None or set(payload) != {"case"}:
            raise WorldError("Native service preparation is frozen")
        task = self.installed.resolve(payload["case"])
        if task.service not in {"filesystem", "postgres"}:
            raise NativeConfigurationError("Native service runtime has not been installed")
        actual = (
            filesystem_asset(self.initial_root, task.category_id)
            if task.service == "filesystem"
            else postgres_asset(Path(os.environ["EVALHIVE_MCPMARK_NATIVE_ROOT"]) / "postgres_state")
        )
        expected = self.installed.receipt["provenance"].get("initial_assets", [])
        if actual not in expected:
            raise NativeConfigurationError("Initial native state differs from its installation")
        self.task = task
        if task.service == "filesystem":
            os.environ["FILESYSTEM_TEST_ROOT"] = str(self.initial_root)
            native = importlib.import_module("src.mcp_services.filesystem.filesystem_state_manager")
            self.state = native.FilesystemStateManager(test_root=self.initial_root)
        else:
            self.state = self.postgres()
        if not self.state.set_up(task):
            raise WorldError("Native state preparation was not confirmed")
        config = self.state.get_service_config_for_agent()
        self.connection_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.connection_root / "connection.json").open("xb") as stream:
            stream.write(canonical({"service": task.service, "config": config}))
        self.ready = True
        return {
            "prompt": self.installed.manager.get_task_instruction(task),
            "service": task.service,
            "initial_asset_digest": actual["source_digest"],
        }

    def postgres(self):
        import psycopg2

        private = read_credentials()
        if private.get("service") != "postgres" or self.effect is None:
            raise WorldError("Native PostgreSQL credentials or transport are unavailable")
        self.secret_values = (private["password"],)
        connection = {
            "host": "postgres",
            "port": 5432,
            "database": "postgres",
            "username": "postgres",
            "password": private["password"],
        }
        for key, value in connection.items():
            os.environ["POSTGRES_" + key.upper()] = str(value)
        deadline = time.monotonic() + 60
        while True:
            try:
                client = psycopg2.connect(
                    host=connection["host"],
                    port=connection["port"],
                    dbname="postgres",
                    user="postgres",
                    password=connection["password"],
                    connect_timeout=3,
                )
                client.close()
                break
            except psycopg2.OperationalError:
                if time.monotonic() >= deadline:
                    raise WorldError("Native PostgreSQL did not become ready") from None
                time.sleep(0.2)
        native = importlib.import_module("src.mcp_services.postgres.postgres_state_manager")
        with restore_transport(self.effect):
            return native.PostgresStateManager(**connection)

    def capture(self, payload):
        if (
            set(payload) != {"messages_sha256", "agent_success"}
            or type(payload["agent_success"]) is not bool
        ):
            raise WorldError("Native message capture is invalid")
        encoded(payload)
        if self.captured is not None:
            if self.captured != payload:
                raise WorldError("Native message capture is frozen")
            return {"captured": True}
        target = self.records_root / "transcript/messages.json"
        data = read_messages(target.parent)
        if hashlib.sha256(data).hexdigest() != payload["messages_sha256"] or not isinstance(
            json.loads(data), list
        ):
            raise WorldError("Native message receipt differs from the confirmed transcript")
        self.state.set_verification_environment(str(target))
        self.captured = json.loads(encoded(payload))
        return {"captured": True}

    def verify(self, payload):
        if payload or self.captured is None:
            raise WorldError("Native verification requires a confirmed message capture")
        if self.verdict is not None:
            return self.verdict
        observed = []
        native = self.installed.manager.run_verification

        def verify(task):
            result = native(task)
            observed.append(result)
            return result

        self.installed.manager.run_verification = verify
        try:
            result = self.installed.manager.execute_task(
                self.task, {"success": self.captured["agent_success"]}
            )
        finally:
            self.installed.manager.run_verification = native
        # The upstream manager catches verifier exceptions and returns false.
        # Preserve that result while distinguishing a checker failure to run.
        value = {
            "native_result": self.redacted(asdict(result)),
            "native_verdict_complete": len(observed) == 1,
            "verifier_exit_code": observed[0].returncode if observed else None,
            "messages_interface": "normalized_harness_messages",
        }
        encoded(value)
        report = self.records_root / "report"
        report.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (report / "verdict.json").open("xb") as stream:
            stream.write(canonical(value))
        self.verdict = value
        return value

    def redacted(self, value):
        if isinstance(value, str):
            for secret in self.secret_values:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, dict):
            return {key: self.redacted(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redacted(item) for item in value]
        return value


def main():
    from .world_control import serve

    # Native checkers print their reports; stdout is never the control channel.
    sys.stdout = sys.stderr
    root = Path("/dataset")
    receipt = root / "installation.json"
    if receipt.is_symlink() or receipt.stat().st_size > MAX_BYTES * 32:
        raise NativeConfigurationError("Native installation metadata is unavailable")

    def factory(effect):
        return NativeService(
            InstalledMCPMark(root / "native", json.loads(receipt.read_text())), effect=effect
        )

    serve(factory)


if __name__ == "__main__":
    main()

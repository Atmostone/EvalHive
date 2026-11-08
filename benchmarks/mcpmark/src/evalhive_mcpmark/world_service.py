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

from .assets import filesystem_asset, github_asset, postgres_asset
from .dataset import InstalledMCPMark
from .native import SERVICES, NativeConfigurationError, canonical, fingerprint
from .postgres_transport import restore_transport
from .world_credentials import read as read_credentials
from .world_rpc import MAX_BYTES, WorldError, encoded
from .world_stage import read as read_messages


class NativePostgresUnavailable(WorldError):
    pass


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
        self.restore_results = []
        self.external_namespace = None
        self.external_mutations_started = False

    def __call__(self, method, payload):
        if method == "cleanup":
            if payload:
                raise WorldError("Native cleanup does not accept parameters")
            if self.closed:
                return {"cleaned": True}
            return self.cleanup()
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
        if task.service not in SERVICES:
            raise NativeConfigurationError("Native service runtime has not been installed")
        initial_digest = None
        if task.service in {"filesystem", "postgres"}:
            actual = (
                filesystem_asset(self.initial_root, task.category_id)
                if task.service == "filesystem"
                else postgres_asset(
                    Path(os.environ["EVALHIVE_MCPMARK_NATIVE_ROOT"]) / "postgres_state"
                )
            )
            expected = self.installed.receipt["provenance"].get("initial_assets", [])
            if actual not in expected:
                raise NativeConfigurationError("Initial native state differs from its installation")
            initial_digest = actual["source_digest"]
        self.task = task
        if task.service == "filesystem":
            os.environ["FILESYSTEM_TEST_ROOT"] = str(self.initial_root)
            native = importlib.import_module("src.mcp_services.filesystem.filesystem_state_manager")
            self.state = native.FilesystemStateManager(test_root=self.initial_root)
        elif task.service == "postgres":
            self.state = self.postgres()
        else:
            self.state, initial_digest = self.external(task)
        self.external_mutations_started = self.external_namespace is not None
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
            "initial_asset_digest": initial_digest,
        }

    def external(self, task):
        from .external_state import create_state

        provenance = self.installed.receipt["provenance"]
        assets = provenance.get("initial_assets", [])
        initial_digest = None
        if task.service == "github":
            if not assets or any(
                asset != github_asset(self.initial_root, asset["category"]) for asset in assets
            ):
                raise NativeConfigurationError(
                    "Initial GitHub templates differ from their installation"
                )
            initial_digest = fingerprint(assets)
        private = {}
        if task.service in {"github", "notion"}:
            configuration = read_credentials()
            if configuration.get("service") != task.service:
                raise NativeConfigurationError(
                    "Native external credentials belong to another service"
                )
            private = configuration["credentials"]
            self.secret_values = tuple(
                private[name]
                for name in ("github_token", "source_key", "evaluation_key")
                if name in private
            )
            if task.service == "notion":
                browser = private.get("browser_state", {})
                self.secret_values += tuple(
                    item["value"]
                    for item in browser.get("cookies", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("value"), str)
                    and item["value"]
                )
                self.secret_values += tuple(
                    item["value"]
                    for origin in browser.get("origins", [])
                    if isinstance(origin, dict)
                    for item in origin.get("localStorage", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("value"), str)
                    and item["value"]
                )
        state = create_state(
            task.service,
            provenance.get("service_settings"),
            private,
            category=task.category_id,
            initial_root=self.initial_root,
            private_root=Path("/control/native-state"),
            effect=self.effect,
        )
        if task.service in {"github", "notion"}:
            from .external_namespace import prepare

            self.external_namespace = prepare(task.service, state)
        return state, initial_digest

    def cleanup(self):
        native_confirmed, namespace_proof = True, None
        namespace_confirmed = self.external_namespace is None
        try:
            if self.state is not None:
                native_confirmed = bool(self.state.clean_up(self.task))
        except Exception:
            native_confirmed = False
        try:
            if self.external_namespace is not None:
                namespace_proof = self.external_namespace.cleanup()
                namespace_confirmed = namespace_proof.get("empty") is True
        except Exception:
            namespace_confirmed = False
        finally:
            close = getattr(self.state, "close_all", None)
            if close is not None:
                close()
        evidence_complete = not self.external_mutations_started or (
            self.ready
            and self.captured is not None
            and self.captured.get("agent_success") is True
            and self.captured.get("tool_effects_complete") is True
        )
        if self.external_namespace is not None:
            report = self.records_root / "report"
            report.mkdir(mode=0o700, parents=True, exist_ok=True)
            (report / "external-cleanup.json").write_bytes(
                canonical(
                    {
                        "namespace": namespace_proof,
                        "native_cleanup_confirmed": native_confirmed,
                        "namespace_cleanup_confirmed": namespace_confirmed,
                        "mutation_evidence_complete": bool(evidence_complete),
                        "cleanup_confirmed": bool(
                            native_confirmed and namespace_confirmed and evidence_complete
                        ),
                    }
                )
            )
        if not native_confirmed or not namespace_confirmed or not evidence_complete:
            raise WorldError("Native cleanup was not confirmed by complete mutation evidence")
        self.closed = True
        return {"cleaned": True}

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
                    raise NativePostgresUnavailable(
                        "Native PostgreSQL did not become ready"
                    ) from None
                time.sleep(0.2)
        native = importlib.import_module("src.mcp_services.postgres.postgres_state_manager")
        with restore_transport(self.restore):
            return native.PostgresStateManager(**connection)

    def restore(self, operation, payload):
        result = self.effect(operation, payload)
        record = {
            "argv": payload["argv"],
            "exit_code": result["exit_code"],
        }
        for name in ("stdout", "stderr"):
            value = self.redacted(result[name])
            record[name] = value[:32768]
            record[name + "_truncated"] = len(value) > 32768
        self.restore_results.append(record)
        report = self.records_root / "report"
        report.mkdir(mode=0o700, parents=True, exist_ok=True)
        (report / "postgres-restore.json").write_bytes(canonical(self.restore_results))
        return result

    def capture(self, payload):
        if (
            not {"messages_sha256", "agent_success"} <= set(payload)
            or set(payload) - {"messages_sha256", "agent_success", "tool_effects_complete"}
            or type(payload["agent_success"]) is not bool
            or (
                "tool_effects_complete" in payload
                and type(payload["tool_effects_complete"]) is not bool
            )
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

import asyncio
import copy
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from evalhive_harness.client import ConnectorClient
from evalhive_runner.api import RetryableDeliveryError
from evalhive_runner.redaction import Redactor
from evalhive_runner.runner import AttemptRunner, RunnerPolicy
from evalhive_runner.spool import EventSpool

ROOT = Path(__file__).parents[2]
FIXTURES = json.loads((ROOT / "sdk/fixtures/conformance.json").read_text())
POLICY = RunnerPolicy(
    heartbeat_seconds=0.02,
    retry_seconds=0.01,
    flush_seconds=2,
    shutdown_seconds=0.2,
    command_seconds=2,
)


class API:
    def __init__(self, *, drop_ack=False, offline=False, commands=False):
        self.records = {}
        self.drop_ack, self.offline, self.commands = drop_ack, offline, commands
        self.sent = set()
        self.acks = {}
        self.calls = 0
        self.ack_dropped = False

    async def events(self, batch):
        self.calls += 1
        if self.offline:
            raise RetryableDeliveryError()
        for record in batch:
            if record["seq"] in self.records:
                assert self.records[record["seq"]] == record
            self.records[record["seq"]] = copy.deepcopy(record)
        if self.drop_ack:
            self.drop_ack = False
            raise RetryableDeliveryError("Response connection lost after server commit")
        through = max(self.records)
        return {"acked_through": through, "next_seq": through + 1}

    async def prepare(self, config):
        self.prepared_config = copy.deepcopy(config)
        return {"accepted": True}

    async def heartbeat(self):
        commands = []
        if self.commands and len(self.records) >= 2:
            if "feedback" not in self.sent:
                self.sent.add("feedback")
                commands = [{"id": "feedback", "name": "feedback", "payload": {"text": "Continue"}}]
            elif "abort" not in self.sent and "feedback" in self.acks:
                self.sent.add("abort")
                commands = [{"id": "abort", "name": "abort", "payload": {}}]
        return {"status": "running", "commands": commands}

    async def acknowledge(self, command_id, response):
        self.acks[command_id] = response
        if command_id == "feedback" and not self.ack_dropped:
            self.ack_dropped = True
            raise RetryableDeliveryError("Command receipt committed but response was lost")
        return {"duplicate": False}


def test_unsupported_interaction_fails_without_waiting_for_a_deadline(tmp_path):
    class Connector:
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        async def request(self, method, *args, **kwargs):
            if method == "describe":
                return FIXTURES["manifest"]
            if method == "validate":
                return {"accepted": True, "effective_config": args[0]["run"]["config"]}
            if method == "start":
                self.started.set()
            return {"accepted": True}

        async def events(self):
            await self.started.wait()
            for seq, (name, data) in enumerate([
                ("run.started", {}),
                ("interaction.request", {"request_id": "input-1", "kind": "question", "payload": {}}),
            ]):
                yield {"protocol_version": "1.0", "type": "event", "attempt_id": FIXTURES["run_spec"]["attempt_id"],
                       "event_id": str(seq), "seq": seq, "source": "fixture", "event": name, "data": data}
            await asyncio.Event().wait()

        async def close(self, **kwargs):
            self.closed = True

    async def scenario():
        connector, api = Connector(), API()
        with EventSpool(tmp_path / "spool.sqlite") as spool:
            runner = AttemptRunner(spec=copy.deepcopy(FIXTURES["run_spec"]), manifest=FIXTURES["manifest"],
                connector=connector, api=api, spool=spool, policy=POLICY)
            result = await asyncio.wait_for(runner.run(), 2)
        assert result["status"] == "failed" and connector.closed
        assert api.records[max(api.records)]["data"]["error"]["code"] == "UNSUPPORTED_INTERACTION"
        assert any(record["event"] == "interaction.request" for record in api.records.values())

    asyncio.run(scenario())


def test_artifact_delivery_precedes_terminal_acknowledgement(tmp_path):
    async def scenario():
        connector = await ConnectorClient.launch(
            [sys.executable, str(ROOT / "sdk/examples/fake/connector.py")]
        )
        spec = copy.deepcopy(FIXTURES["run_spec"])
        api = API(drop_ack=True)

        async def finish(result, emit):
            assert connector.process.returncode == 0
            emit("artifact", {"path": "result.txt", "metadata": {"delivery": "complete"}})
            return {**result, "metadata": {"artifact_delivery": "complete"}}

        with EventSpool(tmp_path / "spool.sqlite") as spool:
            runner = AttemptRunner(
                spec=spec,
                manifest=FIXTURES["manifest"],
                connector=connector,
                api=api,
                spool=spool,
                policy=POLICY,
                finish_hook=finish,
            )
            result = await runner.run()
        assert result["status"] == "success"
        records = [api.records[key] for key in sorted(api.records)]
        assert records[-2]["event"] == "artifact" and records[-1]["event"] == "run.finished"
        assert [record["seq"] for record in records] == list(range(len(records)))
        assert records[-1]["data"]["metadata"]["artifact_delivery"] == "complete"

    asyncio.run(scenario())


@pytest.mark.parametrize("unreachable", [False, True])
def test_applied_settings_are_saved_before_start_and_lost_ack_is_retryable(tmp_path, unreachable):
    class PreparationAPI(API):
        attempts = 0

        async def prepare(self, config):
            self.attempts += 1
            self.prepared_config = copy.deepcopy(config)
            if unreachable or self.attempts == 1:
                raise RetryableDeliveryError("Lost configuration receipt")
            return {"accepted": True, "duplicate": True}

        async def events(self, batch):
            assert self.attempts > 0
            return await super().events(batch)

    async def scenario():
        connector = await ConnectorClient.launch([sys.executable, str(ROOT / "sdk/examples/fake/connector.py")])
        spec = {**copy.deepcopy(FIXTURES["run_spec"]), "config": {}}
        api = PreparationAPI()
        with EventSpool(tmp_path / "spool.sqlite") as spool:
            runner = AttemptRunner(spec=spec, manifest=FIXTURES["manifest"], connector=connector,
                api=api, spool=spool, policy=POLICY)
            result = await runner.run()
        assert api.prepared_config == {"prefix": "", "wait": False, "fail": False}
        records = list(api.records.values())
        if unreachable:
            assert result["status"] == "failed"
            assert records[0]["data"]["harness_started"] is False
            assert records[-1]["data"]["error"]["code"] == "CONFIGURATION_UNAVAILABLE"
        else:
            assert result["status"] == "success" and api.attempts == 2
            assert spec["config"] == api.prepared_config
            assert any(record["event"] == "message.end" for record in records)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "oversized"])
def test_finalization_failure_is_bounded_and_does_not_lose_terminal_delivery(tmp_path, mode):
    async def scenario():
        connector = await ConnectorClient.launch(
            [sys.executable, str(ROOT / "sdk/examples/fake/connector.py")]
        )
        api = API()

        async def finish(result, emit):
            if mode == "timeout":
                await asyncio.sleep(10)
            return {**result, "metadata": {"large": "x" * (1024 * 1024)}}

        with EventSpool(tmp_path / "spool.sqlite") as spool:
            runner = AttemptRunner(
                spec=copy.deepcopy(FIXTURES["run_spec"]),
                manifest=FIXTURES["manifest"],
                connector=connector,
                api=api,
                spool=spool,
                policy=replace(POLICY, finalization_seconds=0.05),
                finish_hook=finish,
            )
            result = await asyncio.wait_for(runner.run(), 3)
        assert result["status"] == "failed" and result["pending_events"] == 0
        terminal = api.records[max(api.records)]
        assert terminal["event"] == "run.finished"
        assert terminal["data"]["error"]["code"] == (
            "ARTIFACT_DELIVERY" if mode == "timeout" else "TERMINAL_RECORD_LIMIT"
        )

    asyncio.run(scenario())


def test_native_artifact_cannot_claim_server_delivery(tmp_path):
    class Connector:
        def __init__(self, spec):
            self.spec = spec
            self.started = asyncio.Event()

        async def request(self, method, *args, **kwargs):
            if method == "describe":
                return FIXTURES["manifest"]
            if method == "validate":
                return {"accepted": True, "effective_config": args[0]["run"]["config"]}
            if method == "start":
                self.started.set()
            return {"accepted": True}

        async def events(self):
            await self.started.wait()
            data = [
                ("run.started", {}),
                (
                    "artifact",
                    {
                        "path": "result.txt",
                        "metadata": {
                            "delivery": "complete",
                            "s3_path": "results/foreign-task/private.txt",
                        },
                    },
                ),
                ("run.finished", {"status": "success", "summary": "Done"}),
            ]
            for seq, (name, payload) in enumerate(data):
                yield {
                    "protocol_version": "1.0",
                    "type": "event",
                    "attempt_id": self.spec["attempt_id"],
                    "event_id": str(seq),
                    "seq": seq,
                    "source": "runner",
                    "event": name,
                    "data": payload,
                }

        async def close(self, **kwargs):
            return None

    async def scenario():
        spec = copy.deepcopy(FIXTURES["run_spec"])
        api = API()
        with EventSpool(tmp_path / "spool.sqlite") as spool:
            runner = AttemptRunner(
                spec=spec,
                manifest=FIXTURES["manifest"],
                connector=Connector(spec),
                api=api,
                spool=spool,
                policy=POLICY,
            )
            assert (await runner.run())["status"] == "success"
        artifact = api.records[1]["data"]["metadata"]
        assert artifact["delivery"] == "announced" and "s3_path" not in artifact

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize("mode", ["success", "failure", "feedback_abort", "deadline"])
def test_real_connector_process_supervision(tmp_path, language, mode):
    async def scenario():
        argv = (
            [sys.executable, str(ROOT / "sdk/examples/fake/connector.py")]
            if language == "python"
            else ["node", str(ROOT / "sdk/examples/fake/connector.mjs")]
        )
        connector = await ConnectorClient.launch(argv)
        spec = copy.deepcopy(FIXTURES["run_spec"])
        spec["config"].update(wait=mode in ("feedback_abort", "deadline"), fail=mode == "failure")
        spec["limits"]["wall_time_seconds"] = 1 if mode == "deadline" else 10
        api = API(drop_ack=True, commands=mode == "feedback_abort")
        with EventSpool(tmp_path / "private/spool.sqlite") as spool:
            runner = AttemptRunner(
                spec=spec,
                manifest=FIXTURES["manifest"],
                connector=connector,
                api=api,
                spool=spool,
                policy=POLICY,
            )
            try:
                result = await asyncio.wait_for(runner.run(), 12)
            finally:
                await connector.close(grace=0.2)
            expected = {
                "success": "success",
                "failure": "failed",
                "feedback_abort": "cancelled",
                "deadline": "failed",
            }[mode]
            assert result["status"] == expected and result["pending_events"] == 0
            records = [api.records[key] for key in sorted(api.records)]
            assert [record["seq"] for record in records] == list(range(len(records)))
            assert records[0]["event"] == "run.started" and records[-1]["event"] == "run.finished"
            assert len({record["event_id"] for record in records}) == len(records)
            if mode == "feedback_abort":
                assert set(api.acks) == {"feedback", "abort"}
                assert any(record["data"].get("message_id") == "feedback" for record in records)
                assert (
                    sum(record["data"].get("message_id") == "feedback" for record in records) == 1
                )
            if mode == "deadline":
                assert records[-1]["data"]["error"]["code"] == "DEADLINE"
            assert api.calls >= 2

    asyncio.run(scenario())


def test_noncooperative_abort_has_a_bounded_shutdown(tmp_path):
    class NoncooperativeConnector:
        def __init__(self, spec):
            self.spec, self.queue, self.seq, self.closed = spec, asyncio.Queue(), 0, False

        async def emit(self, name, data):
            await self.queue.put(
                {
                    "protocol_version": "1.0",
                    "type": "event",
                    "attempt_id": self.spec["attempt_id"],
                    "event_id": f"event-{self.seq}",
                    "seq": self.seq,
                    "source": "test",
                    "event": name,
                    "data": data,
                }
            )
            self.seq += 1

        async def request(self, method, params=None, **_):
            if method == "describe":
                return FIXTURES["manifest"]
            if method == "validate":
                return {"accepted": True, "effective_config": params["run"]["config"]}
            if method == "start":
                await self.emit("run.started", {})
                await self.emit("message.end", {"message_id": "start", "content": "started"})
            if method == "control" and params["command"] == "feedback":
                await self.emit(
                    "message.end", {"message_id": "feedback", "content": params["payload"]["text"]}
                )
            return {"accepted": True}

        async def events(self):
            while (event := await self.queue.get()) is not None:
                yield event

        async def close(self, **_):
            self.closed = True
            await self.queue.put(None)

    async def scenario():
        spec = copy.deepcopy(FIXTURES["run_spec"])
        connector = NoncooperativeConnector(spec)
        api = API(commands=True)
        with EventSpool(tmp_path / "spool.sqlite") as spool:
            runner = AttemptRunner(
                spec=spec,
                manifest=FIXTURES["manifest"],
                connector=connector,
                api=api,
                spool=spool,
                policy=RunnerPolicy(heartbeat_seconds=0.01, shutdown_seconds=0.05, flush_seconds=1),
            )
            result = await asyncio.wait_for(runner.run(), 2)
            assert result["status"] == "cancelled" and connector.closed
            terminal = api.records[max(api.records)]
            assert terminal["source"] == "runner"
            assert terminal["data"]["metadata"]["runner_elapsed_seconds"] is None

    asyncio.run(scenario())


def test_abort_before_start_does_not_execute_the_harness(tmp_path):
    class CancelBeforeStart(API):
        async def heartbeat(self):
            return {
                "status": "stopping",
                "commands": [{"id": "abort", "name": "abort", "payload": {}}],
            }

    async def scenario():
        connector = await ConnectorClient.launch(
            [sys.executable, str(ROOT / "sdk/examples/fake/connector.py")]
        )
        api = CancelBeforeStart()
        with EventSpool(tmp_path / "spool.sqlite") as spool:
            runner = AttemptRunner(
                spec=copy.deepcopy(FIXTURES["run_spec"]),
                manifest=FIXTURES["manifest"],
                connector=connector,
                api=api,
                spool=spool,
                policy=POLICY,
            )
            result = await asyncio.wait_for(runner.run(), 3)
            assert result["status"] == "cancelled"
            assert api.records[0]["data"] == {"harness_started": False}
            assert not any(
                record["event"].startswith("message.") for record in api.records.values()
            )
            assert api.acks["abort"]["result"]["mechanism"] == "before_start"

    asyncio.run(scenario())


def test_offline_delivery_keeps_private_configuration_out_of_the_recoverable_journal(tmp_path):
    async def scenario():
        connector = await ConnectorClient.launch(
            [sys.executable, str(ROOT / "sdk/examples/fake/connector.py")]
        )
        spec = copy.deepcopy(FIXTURES["run_spec"])
        spec["config"] = {"prefix": "test-private-key"}
        api = API(offline=True)
        path = tmp_path / "private/spool.sqlite"
        with EventSpool(path) as spool:
            runner = AttemptRunner(
                spec=spec,
                manifest=FIXTURES["manifest"],
                connector=connector,
                api=api,
                spool=spool,
                redactor=Redactor(["test-private-key"]),
                policy=RunnerPolicy(flush_seconds=0.05, retry_seconds=0.01, shutdown_seconds=0.2),
            )
            result = await runner.run()
            assert result["status"] == "delivery_pending"
        with EventSpool(path) as recovered:
            batch = recovered.batch()
            assert "test-private-key" not in json.dumps(batch)
            assert batch[-1]["data"]["error"]["code"] == "PRIVATE_CONFIGURATION"
            assert batch[0]["data"]["harness_started"] is False
            assert batch[-1]["data"]["status"] == "failed"
            api.offline = False
            receipt = await api.events(batch)
            recovered.acknowledge(receipt["acked_through"])
            assert recovered.pending_count == 0

    asyncio.run(scenario())


def test_bearer_resource_credentials_are_redacted_with_or_without_scheme():
    from evalhive_runner.redaction import Redactor

    redactor = Redactor(['Bearer private-knowledge-fixture'])
    assert redactor.text('Bearer private-knowledge-fixture / private-knowledge-fixture') == '[REDACTED] / [REDACTED]'

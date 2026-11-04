import asyncio
import copy
import json
import sys
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


def test_offline_delivery_keeps_a_redacted_recoverable_journal(tmp_path):
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
            assert any("[REDACTED]" in str(record) for record in batch)
            assert batch[-1]["data"]["status"] == "success"
            api.offline = False
            receipt = await api.events(batch)
            recovered.acknowledge(receipt["acked_through"])
            assert recovered.pending_count == 0

    asyncio.run(scenario())

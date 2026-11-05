import asyncio
import json

import pytest
from evalhive_benchmark import ProtocolError, command, decode_line, encode_line
from evalhive_benchmark.peer import Peer


def test_timeout_replay_does_not_repeat_native_side_effect():
    async def scenario():
        inbound, outbound = asyncio.Queue(), asyncio.Queue()
        calls = 0
        gate = asyncio.Event()

        async def write(frame):
            outbound.put_nowait(frame)

        async def handle(value):
            nonlocal calls
            calls += 1
            await gate.wait()
            return {"applied": calls}

        async def frames():
            while frame := await inbound.get():
                yield frame

        peer = Peer(write, handler=handle)
        running = asyncio.create_task(peer.read(frames()))
        frame = encode_line(command("same-operation", "describe"))
        inbound.put_nowait(frame)
        inbound.put_nowait(frame)
        await asyncio.sleep(0.01)
        assert calls == 1
        gate.set()
        assert decode_line(await outbound.get())["result"] == {"applied": 1}
        assert decode_line(await outbound.get())["result"] == {"applied": 1}
        inbound.put_nowait(b"")
        await running

    asyncio.run(scenario())


def test_receipt_capacity_does_not_evict_and_reexecute():
    async def scenario():
        sent = []

        async def write(frame):
            sent.append(decode_line(frame))

        async def handle(value):
            return {"applied": True}

        async def frames():
            for identity in ("first", "second", "first"):
                yield encode_line(command(identity, "describe"))
                await asyncio.sleep(0.01)

        await Peer(write, handler=handle, receipt_limit=1).read(frames())
        assert sent[0]["ok"] and sent[2] == sent[0]
        assert sent[1]["error"]["code"] == "LEDGER_FULL"

    asyncio.run(scenario())


def test_uncertain_request_is_explicit():
    async def scenario():
        async def write(frame):
            pass

        peer = Peer(write)
        with pytest.raises(ProtocolError) as caught:
            await peer.request(command("request", "describe"), timeout=0.01)
        assert caught.value.code == "COMMAND_UNCERTAIN"
        assert not peer.pending

    asyncio.run(scenario())


def test_unknown_protocol_returns_an_explicit_error():
    async def scenario():
        sent = []

        async def write(frame):
            sent.append(decode_line(frame))

        async def handle(value):
            raise AssertionError("Invalid input must not reach the connector")

        async def frames():
            value = command("request", "describe")
            value["protocol_version"] = "9.0"
            yield (json.dumps(value) + "\n").encode()

        with pytest.raises(ProtocolError) as caught:
            await Peer(write, handler=handle).read(frames())
        assert caught.value.code == "UNSUPPORTED_PROTOCOL"
        assert sent[0]["error"]["code"] == "UNSUPPORTED_PROTOCOL"

    asyncio.run(scenario())


def test_handler_exception_does_not_expose_private_details():
    async def scenario():
        sent = []

        async def write(frame):
            sent.append(decode_line(frame))

        async def handle(value):
            raise RuntimeError("private-example-value")

        async def frames():
            yield encode_line(command("first", "describe"))
            await asyncio.sleep(0.01)

        await Peer(write, handler=handle).read(frames())
        assert sent[0]["error"]["code"] == "CONNECTOR_ERROR"
        assert "private-example-value" not in json.dumps(sent)

    asyncio.run(scenario())


def test_acknowledgement_reclaims_receipt_without_repeating_the_operation():
    async def scenario():
        sent = []
        calls = 0

        async def write(frame):
            sent.append(decode_line(frame))

        async def handle(value):
            nonlocal calls
            calls += 1
            return {"applied": calls}

        async def frames():
            yield encode_line(command("first", "describe"))
            await asyncio.sleep(0.01)
            ack = {
                "protocol_version": "1.0",
                "type": "receipt_ack",
                "id": "first",
                "request_type": "command",
            }
            yield encode_line(ack)
            yield encode_line(ack)
            yield encode_line(command("second", "describe"))
            await asyncio.sleep(0.01)
            yield encode_line(command("first", "describe"))
            await asyncio.sleep(0.01)

        peer = Peer(write, handler=handle, receipt_limit=1)
        await peer.read(frames())
        assert calls == 2
        assert sent[1]["result"] == {"applied": 2}
        assert sent[2]["error"]["code"] == "RECEIPT_RETIRED"
        assert peer.receipts[("command", "first")][1] is None

    asyncio.run(scenario())

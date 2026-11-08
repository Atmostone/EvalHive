"""Lifecycle framing and exec authority are independent of a particular harness."""

import asyncio
import socket
import struct
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.runtime.managed_exec import (
    SUPERVISOR,
    OwnedDockerProcess,
    OwnedExecRuntime,
    SupervisorOutput,
    _Output,
)
from app.runtime.managed_spec import (
    ExecSpec,
    ManagedContainer,
    ManagedRuntimeError,
    OwnerScope,
)


def identity():
    return str(uuid.uuid4())


@pytest.fixture
def owned():
    scope = OwnerScope(identity(), identity(), identity(), 1)
    container = ManagedContainer(scope, identity(), "container", "sha256:" + "a" * 64)
    spec = ExecSpec(container, identity(), ("worker",), private_inputs={"CONFIG": b"private-value"})
    runtime = MagicMock()
    native = runtime.container.return_value
    native.status = "running"
    native.attrs = {"Mounts": [{"Type": "bind", "RW": False, "Destination": "/evalhive-runtime"}]}
    execution = OwnedExecRuntime(runtime)
    runtime.client.api.exec_create.return_value = {"Id": "exec"}
    handle = execution.create(spec)
    runtime.client.api.exec_inspect.return_value = {
        "ContainerID": container.container_id,
        "Running": True,
        "ExitCode": 0,
        "ProcessConfig": {"entrypoint": handle.argv[0], "arguments": list(handle.argv[1:])},
    }
    return execution, runtime, spec, handle


def record(parser, kind, code, children=0):
    return parser.prefix + f"{kind} {code} {children}\n".encode()


def test_lifecycle_framing_at_every_chunk_boundary():
    key = bytes(range(32))
    sample = SupervisorOutput(key)
    payload = (
        record(sample, "READY", 41) + b'{"message":"complete"}\n' + record(sample, "EXIT", 7, 2)
    )
    for boundary in range(len(payload) + 1):
        parser = SupervisorOutput(key)
        result = parser.feed(payload[:boundary]) + parser.feed(payload[boundary:])
        assert result == b'{"message":"complete"}\n'
        assert parser.ready and parser.exit_code == 7 and parser.children == 2
        assert not parser.buffer
    parser = SupervisorOutput(key)
    assert b"".join(parser.feed(bytes([value])) for value in payload) == b'{"message":"complete"}\n'
    assert parser.exit_code == 7


def test_complete_lines_are_not_held_for_a_possible_future_marker():
    parser = SupervisorOutput(b"a" * 32)
    parser.feed(record(parser, "READY", 41))
    assert parser.feed(b"{}\n") == b"{}\n"
    assert parser.feed(b"x\x1eEVAL") == b"x"
    assert parser.feed(b"unrelated\n") == b"\x1eEVALunrelated\n"
    # An agent cannot forge native completion without the private key.
    other = SupervisorOutput(b"b" * 32)
    assert parser.feed(record(other, "EXIT", 0)) == record(other, "EXIT", 0)
    assert parser.exit_code is None


@pytest.mark.parametrize("kind,code,children", [("EXIT", 0, 0), ("READY", 0, 0), ("READY", 1, 1)])
def test_invalid_initial_lifecycle_is_rejected(kind, code, children):
    parser = SupervisorOutput(b"a" * 32)
    with pytest.raises(ManagedRuntimeError):
        parser.feed(record(parser, kind, code, children))


def test_duplicate_completion_and_oversized_records_are_rejected():
    parser = SupervisorOutput(b"a" * 32)
    parser.feed(record(parser, "READY", 41) + record(parser, "EXIT", 0))
    with pytest.raises(ManagedRuntimeError):
        parser.feed(record(parser, "EXIT", 0))
    parser = SupervisorOutput(b"a" * 32)
    with pytest.raises(ManagedRuntimeError):
        parser.feed(parser.prefix + b"x" * 257)


def test_private_inputs_and_key_are_not_docker_arguments_or_environment(owned):
    execution, runtime, spec, handle = owned
    declaration = runtime.client.api.exec_create.call_args.kwargs
    assert "private-value" not in repr(declaration)
    assert spec.control_key.hex() not in repr(declaration)
    assert "private-value" not in repr(spec) + repr(handle)
    assert declaration["cmd"][:4] == [SUPERVISOR, "--socket", handle.argv[2], "--private-env"]
    assert declaration["environment"] == {}
    assert execution.inspect(handle)["running"]


@pytest.mark.parametrize("field", ["ContainerID", "entrypoint", "arguments"])
def test_foreign_exec_identity_is_rejected_before_control(owned, field):
    execution, runtime, _, handle = owned
    inspected = runtime.client.api.exec_inspect.return_value
    if field == "ContainerID":
        inspected[field] = "foreign"
    else:
        inspected["ProcessConfig"][field] = [] if field == "arguments" else "foreign"
    runtime.client.api.exec_create.reset_mock()
    with pytest.raises(ManagedRuntimeError, match="does not belong"):
        execution.control(handle, "stop")
    runtime.client.api.exec_create.assert_not_called()


def test_container_authority_and_readonly_bundle_are_required(owned):
    execution, runtime, spec, _ = owned
    runtime.container.return_value.attrs["Mounts"][0]["RW"] = True
    with pytest.raises(ManagedRuntimeError, match="read-only"):
        execution.create(spec)
    runtime.container.side_effect = ManagedRuntimeError("foreign generation")
    with pytest.raises(ManagedRuntimeError, match="foreign generation"):
        execution.create(spec)


@pytest.mark.parametrize(
    "changes",
    [
        {"control_key": b"short"},
        {"argv": ()},
        {"argv": ("bad\0value",)},
        {"private_inputs": {"INVALID=NAME": b"value"}},
        {"private_inputs": {"CONFIG": b"x" * (1024 * 1024 + 1)}},
        {"environment": {"CONFIG": "public"}},
        {"workdir": "/workspace/../root"},
    ],
)
def test_invalid_exec_intent_is_rejected_before_creation(owned, changes):
    execution, runtime, spec, _ = owned
    runtime.client.api.exec_create.reset_mock()
    with pytest.raises(ManagedRuntimeError):
        execution.create(replace(spec, **changes))
    runtime.client.api.exec_create.assert_not_called()


def test_reconciliation_never_blindly_restarts_a_native_process(owned):
    execution, runtime, spec, handle = owned
    runtime.container.return_value.attrs["ExecIDs"] = [handle.exec_id]
    assert execution.find(spec) == handle
    runtime.container.return_value.attrs["ExecIDs"] = [handle.exec_id, "second"]
    with pytest.raises(ManagedRuntimeError, match="conflicting"):
        execution.find(spec)
    runtime.client.api.exec_start.assert_not_called()


def test_private_input_declaration_survives_json_object_reordering(owned):
    execution, _, spec, _ = owned
    first = replace(spec, private_inputs={"Z_CONFIG": b"zeta", "A_CONFIG": b"alpha"})
    second = replace(spec, private_inputs={"A_CONFIG": b"alpha", "Z_CONFIG": b"zeta"})
    assert execution._argv(first) == execution._argv(second)
    assert execution._argv(first)[3:7] == ("--private-env", "A_CONFIG", "--private-env", "Z_CONFIG")


@pytest.mark.asyncio
async def test_raw_native_output_can_exceed_a_jsonl_record_without_unbounded_capture():
    stream = _Output()
    await stream.queue.put(b"x" * (1024 * 1024 + 10))
    await stream.queue.put(None)
    total = 0
    while value := await stream.read(65536):
        assert len(value) <= 65536
        total += len(value)
    assert total == 1024 * 1024 + 10


@pytest.mark.asyncio
async def test_sdk_stream_eof_is_native_completion_while_wrapper_is_retained():
    server, client = socket.socketpair()
    runtime = SimpleNamespace(control=MagicMock(), runtime=MagicMock())
    handle = SimpleNamespace(control_key=b"k" * 32, container="container")
    process = OwnedDockerProcess(runtime, handle, client)

    def frame(data, kind=1):
        server.sendall(bytes([kind, 0, 0, 0]) + struct.pack(">I", len(data)) + data)

    try:
        frame(record(process.records, "READY", 42))
        frame(b'{"ok":true}\n')
        assert await asyncio.wait_for(process.stdout.readline(), 1) == b'{"ok":true}\n'
        assert process.returncode is None
        frame(record(process.records, "EXIT", 3, 1))
        assert await asyncio.wait_for(process.wait(), 1) == 3
        assert await asyncio.wait_for(process.stdout.readline(), 1) == b""
        assert not process.environment_broken
        runtime.control.assert_not_called()  # Native exit does not release the lease.
    finally:
        await process.aclose()
        server.close()


@pytest.mark.asyncio
async def test_truncated_transport_stops_only_the_owned_environment():
    server, client = socket.socketpair()
    runtime = SimpleNamespace(control=MagicMock(), runtime=MagicMock())
    handle = SimpleNamespace(control_key=b"k" * 32, container="owned-container")
    process = OwnedDockerProcess(runtime, handle, client)
    try:
        server.close()
        with pytest.raises(ManagedRuntimeError, match="unsafe to grade"):
            await asyncio.wait_for(process.wait(), 1)
        assert process.environment_broken
        runtime.runtime.stop.assert_called_once_with("owned-container", grace_seconds=0)
    finally:
        await process.aclose()


@pytest.mark.asyncio
async def test_protocol_line_limit_applies_even_when_newline_is_present():
    stream = _Output()
    await stream.queue.put(b"x" * (1024 * 1024 + 1) + b"\n")
    with pytest.raises(ManagedRuntimeError, match="size limit"):
        await stream.readline()

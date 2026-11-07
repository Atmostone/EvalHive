"""Private message transfer detects gaps, conflicts and altered receipts."""

import base64
import hashlib

import pytest
from evalhive_mcpmark.world_stage import execute


def test_repeated_message_chunks_are_idempotent_and_sealed_content_is_frozen(tmp_path):
    data = b'[{"type":"message"}]'
    first = {"offset": 0, "data": base64.b64encode(data[:10]).decode()}
    assert execute("write", first, root=tmp_path) == execute("write", first, root=tmp_path)
    with pytest.raises(ValueError, match="conflicts"):
        execute("write", {**first, "data": base64.b64encode(b"changed!!!").decode()}, root=tmp_path)
    with pytest.raises(ValueError, match="order"):
        execute(
            "write", {"offset": 11, "data": base64.b64encode(data[10:]).decode()}, root=tmp_path
        )
    execute("write", {"offset": 10, "data": base64.b64encode(data[10:]).decode()}, root=tmp_path)
    proof = {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with pytest.raises(ValueError, match="incomplete"):
        execute("seal", {**proof, "sha256": "0" * 64}, root=tmp_path)
    assert execute("seal", proof, root=tmp_path) == execute("seal", proof, root=tmp_path) == proof
    with pytest.raises(ValueError, match="frozen"):
        execute("write", first, root=tmp_path)

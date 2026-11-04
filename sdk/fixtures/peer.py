"""Adversarial wire peer for both host SDKs; deliberately bypasses serve()."""

import json
import sys
import time
from pathlib import Path

from evalhive_harness.protocol import encode_line, response

fixtures = json.loads(Path(__file__).with_name("conformance.json").read_text())


def read():
    return json.loads(sys.stdin.buffer.readline())


def reply(request):
    result = (
        fixtures["manifest"] if request["method"] == "describe" else {"method": request["method"]}
    )
    sys.stdout.buffer.write(encode_line(response(request["id"], result)))
    sys.stdout.buffer.flush()


first = read()
if sys.argv[1] == "reorder":
    second = read()
    reply(second)
    reply(first)
elif sys.argv[1] == "late":
    time.sleep(0.2)
    reply(first)
    reply(read())
elif sys.argv[1] == "flood":
    for i in range(3):
        sys.stdout.buffer.write(
            encode_line(
                {
                    "protocol_version": "1.0",
                    "type": "event",
                    "event_id": str(i),
                    "attempt_id": "attempt-1",
                    "seq": i,
                    "source": "test",
                    "event": "heartbeat",
                    "data": {},
                }
            )
        )
    sys.stdout.buffer.flush()
elif sys.argv[1] == "hang":
    time.sleep(60)
else:
    raise ValueError("Unknown peer scenario")
# Wait for close without inventing more protocol messages.
sys.stdin.buffer.read()

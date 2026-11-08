"""Seal the private world and confirm native cleanup through a stable receipt."""

import json
import os
import sys
import time

from .world_control import exchange
from .world_rpc import monotonic


def finalize(scope, *, timeout=45, control=exchange):
    scope = {key: scope[key] for key in ("workspace_id", "trial_id", "generation")}
    request = {"id": "host.finalize", "scope": scope, "operation": "finalize", "payload": {}}
    started = control(request)
    deadline = monotonic() + timeout
    index = 0
    while monotonic() < deadline:
        result = control(
            {
                "id": "host.finalize.poll." + str(index),
                "scope": scope,
                "operation": "poll",
                "payload": {"operation_id": started["operation_id"]},
            }
        )
        if result["state"] == "completed":
            if result["result"] != {"cleaned": True}:
                raise ValueError("Native cleanup did not confirm its result")
            return {"finalized": True}
        if result["state"] == "failed" or result["effects"]:
            raise ValueError("Native finalization was not confirmed")
        index += 1
        time.sleep(0.05)
    raise TimeoutError("Native finalization exceeded its deadline")


def main():
    try:
        scope = json.loads(os.environ.pop("EVALHIVE_RESOURCE_SCOPE"))
        timeout = float(os.environ.pop("EVALHIVE_FINALIZER_TIMEOUT"))
        if not 0 < timeout <= 600:
            raise ValueError("Native finalizer requires a bounded host deadline")
        print(json.dumps(finalize(scope, timeout=timeout), separators=(",", ":")))
    except Exception:
        sys.stderr.write("Native cleanup was not confirmed\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

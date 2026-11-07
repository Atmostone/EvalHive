"""Offline container probe of native API isolation, persistence and evaluation."""

import http.client
import json
import subprocess
import sys
from pathlib import Path

from evalhive_appworld.dataset import InstalledAppWorld
from evalhive_appworld.world_service import NativeService


def request(method, path, value=None):
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=30)
    try:
        connection.request(
            method,
            path,
            body=None if value is None else json.dumps(value),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None
    finally:
        connection.close()


def main():
    receipt = json.loads(Path("/probe/installation.json").read_text())
    dataset = InstalledAppWorld("/appworld", receipt)
    service = NativeService(dataset, connection="/evalhive-connection")
    try:
        prepared = service("prepare", {"case": receipt["cases"][0]})
        assert sum(group["tool_count"] for group in prepared["groups"]) == 469
        assert request("GET", "/supervisor/active_task")[0] == 200
        for path in ("/admin", "/dbs", "/__evalhive/open", "/.__evalhive/open", "/docs"):
            assert request("POST", path, {})[0] == 403
        if "--mcp" in sys.argv:
            subprocess.run([sys.executable, "/probe/client_probe.py"], check=True, timeout=180)
        else:
            assert (
                request(
                    "POST",
                    "/supervisor/message",
                    {"status": "success", "answer": "Engineering fixture"},
                )[0]
                == 200
            )
        assert service.world.models is None
        if "--cancel" in sys.argv:
            assert service("cleanup", {}) == {"cleaned": True}
            proof = json.loads((service.records / "persistence.json").read_text())
            assert proof["saved"] and proof["public_api_sealed"]
            assert any(
                b"Engineering fixture" in path.read_bytes()
                for path in Path(service.world.output_db_home_path_on_disk).rglob("*")
                if path.is_file() and "supervisor" in path.name
            )
            print(json.dumps({"cancellation_persisted": True, "cleanup": True}), flush=True)
            return
        captured = service("capture", {"agent_success": True})
        assert captured["saved"]
        assert request("GET", "/supervisor/active_task")[0] == 403
        assert list(Path(service.world.output_db_home_path_on_disk).glob("*"))
        verdict = service("verify", {})
        assert verdict["native_verdict_complete"] and verdict["saved_before_evaluation"]
        assert type(verdict["success"]) is bool
        assert service("cleanup", {}) == service("cleanup", {}) == {"cleaned": True}
        print(
            json.dumps(
                {
                    "native_api": True,
                    "public_controls_hidden": True,
                    "saved_before_evaluation": True,
                    "native_checker": True,
                    "success": verdict["success"],
                    "cleanup": True,
                }
            )
        )
    finally:
        service("cleanup", {})


if __name__ == "__main__":
    main()

"""Native site commands cannot select another image, container or host port."""

import asyncio

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_mcpmark.webarena_transport import SiteTransport

SITE = {"container_alias": "shopping_admin", "image_alias": "shopping_admin_final_0719", "port": 80}


def test_native_site_lifecycle_uses_one_scoped_resource_and_repeatable_cleanup():
    calls, handles = [], {}

    async def broker(operation, payload, **options):
        calls.append((operation, payload, options))
        if operation == "allocate":
            return {"handle": "owned-site"}
        if operation == "inspect":
            return {"native": {"status": "running"}}
        return {"exit_code": 0, "stdout": "native output", "stderr": ""}

    async def run():
        transport = SiteTransport(SITE, broker, remaining=lambda: 30, handles=handles)
        assert (await transport.command(["docker", "stop", "shopping_admin"]))["exit_code"] == 1
        assert not calls
        command = [
            "docker",
            "run",
            "--name",
            "shopping_admin",
            "-p",
            "80:80",
            "-d",
            "shopping_admin_final_0719",
        ]
        assert (await transport.command(command))["stdout"] == "owned-site\n"
        with pytest.raises(ProtocolError, match="twice"):
            await transport.command(command)
        inspected = await transport.command(
            ["docker", "ps", "--filter", "name=^shopping_admin$", "--format", "{{.Names}}"]
        )
        assert inspected["stdout"] == "shopping_admin\n"
        assert (
            await transport.command(["docker", "exec", "shopping_admin", "mysql", "--version"])
        )["stdout"] == "native output"
        for _ in range(2):
            await transport.command(["docker", "stop", "shopping_admin"])
            await transport.command(["docker", "rm", "shopping_admin"])
        assert [call[0] for call in calls] == ["allocate", "inspect", "exec", "stop", "release"]
        assert all(call[2] == {"resource": "site"} for call in calls[1:])

    asyncio.run(run())


@pytest.mark.parametrize(
    "command",
    [
        ["docker", "exec", "owner-postgres", "psql"],
        ["docker", "run", "--privileged", "unregistered"],
        ["docker", "load", "--input", "/owner/image.tar"],
        ["docker", "rm", "owner-service"],
    ],
)
def test_native_site_cannot_reach_unregistered_daemon_resources(command):
    async def broker(*args, **kwargs):
        raise AssertionError("Rejected commands cannot reach the broker")

    transport = SiteTransport(SITE, broker, remaining=lambda: 30, handles={})
    with pytest.raises(ProtocolError, match="outside"):
        asyncio.run(transport.command(command))

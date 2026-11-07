"""Native resources retain transport semantics without harness-specific branches."""

import pytest
from evalhive_harbor.native import HarborConfigurationError, parse_task
from evalhive_harbor.resources import agent_resources
from test_native import task


def test_native_stdio_and_skills_stay_inside_task_filesystem(tmp_path):
    native = parse_task(
        task(
            tmp_path,
            """[environment]
skills_dir="skills"
[[environment.mcp_servers]]
name="native_tools"
transport="stdio"
command="python"
args=["server.py", "two words"]
""",
        )
    )
    resources = agent_resources(native, "/workspace")
    assert resources == [
        {"id": "tools", "kind": "tools", "config": {"names": ["bash", "mcp__native_tools__*"]}},
        {
            "id": "native_tools",
            "kind": "mcp",
            "config": {
                "transport": "stdio",
                "command": "python",
                "args": ["server.py", "two words"],
                "cwd": "/workspace",
            },
        },
        {"id": "native-skills", "kind": "skill", "config": {"path": "/workspace/skills"}},
    ]


@pytest.mark.parametrize("transport", ["http", "streamable-http"])
def test_streamable_http_retains_public_headers(tmp_path, transport):
    native = parse_task(
        task(
            tmp_path,
            """[[environment.mcp_servers]]
name="remote"
transport="TRANSPORT"
url="http://native-service:8080/mcp"
headers={X-Native-Mode="fixture"}
""".replace("TRANSPORT", transport),
        )
    )
    assert agent_resources(native, "/")[1]["config"] == {
        "transport": "http",
        "url": "http://native-service:8080/mcp",
        "headers": {"X-Native-Mode": "fixture"},
    }


@pytest.mark.parametrize(
    "extra",
    [
        '[[environment.mcp_servers]]\nname="sse"\nurl="http://native/mcp"\n',
        '[[environment.mcp_servers]]\nname="native.name"\ntransport="stdio"\ncommand="native"\n',
        '[[environment.mcp_servers]]\nname="remote"\ntransport="http"\nurl="https://native/mcp"\nheaders={Authorization="Bearer ${TOKEN}"}\n',
        '[environment]\nskills_dir="../private"\n',
        '[[environment.mcp_servers]]\nname="tools"\ntransport="stdio"\ncommand="native"\n',
        '[[environment.mcp_servers]]\nname="remote"\ntransport="http"\nurl="https://user:pass@native/mcp"\n',
    ],
)
def test_unsupported_native_resource_semantics_are_not_silently_dropped(tmp_path, extra):
    native = parse_task(task(tmp_path, extra))
    with pytest.raises(HarborConfigurationError):
        agent_resources(native, "/workspace")

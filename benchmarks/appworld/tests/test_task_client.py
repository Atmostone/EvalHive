"""A task cannot select another API server or acquire administrative tools."""

import json
from copy import deepcopy

import pytest
from evalhive_appworld.task_client import read_configuration


@pytest.fixture
def connection():
    return {
        "format_version": "1.0",
        "service": "appworld",
        "source_digest": "sha256:" + "a" * 64,
        "api_url": "http://appworld:8080",
        "groups": [
            {"id": "appworld0", "apps": ["spotify", "supervisor"], "tool_count": 98},
        ],
    }


def test_public_connection_is_read_from_a_bounded_regular_file(tmp_path, connection):
    path = tmp_path / "connection.json"
    path.write_text(json.dumps(connection))
    assert read_configuration(path) == connection
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_configuration(link)


@pytest.mark.parametrize(
    "change",
    [
        {"api_url": "http://127.0.0.1:9000"},
        {"api_url": "http://owner-service:8080"},
        {"groups": [{"id": "appworld0", "apps": ["admin"], "tool_count": 1}]},
        {"groups": [{"id": "appworld0", "apps": ["spotify", "spotify"], "tool_count": 2}]},
        {"groups": [{"id": "appworld0", "apps": ["spotify"], "tool_count": 257}]},
    ],
)
def test_uninstalled_or_private_connections_are_rejected(tmp_path, connection, change):
    value = {**deepcopy(connection), **change}
    path = tmp_path / "connection.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        read_configuration(path)

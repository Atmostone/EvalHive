"""Administrator image inspection must not execute code or expose image settings."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from docker.errors import NotFound
from evalhive_harbor.images import RESERVED_PATHS, inspect_image
from evalhive_harbor.native import HarborConfigurationError

IMAGE = "sha256:" + "a" * 64


def fixture(*, occupied=None):
    stream = Mock()

    def archive(path):
        if path == occupied:
            return stream, {}
        raise NotFound("Fixture path is absent")

    container = SimpleNamespace(get_archive=Mock(side_effect=archive), remove=Mock(), start=Mock())
    image = SimpleNamespace(
        id=IMAGE,
        attrs={
            "Config": {
                "User": "1000:1000",
                "WorkingDir": "/native",
                "Env": ["FIXTURE_PRIVATE=not-for-output"],
            }
        },
    )
    client = SimpleNamespace(
        images=SimpleNamespace(get=Mock(return_value=image)),
        containers=SimpleNamespace(create=Mock(return_value=container)),
    )
    return client, container, stream


def test_inspection_reads_only_required_metadata_and_never_starts_the_image():
    client, container, _ = fixture()
    result = inspect_image(
        client, IMAGE, limits={"memory_mb": 128, "cpu_cores": 0.25, "disk_mb": 64}
    )
    assert result["image"] == IMAGE and result["image_user"] == "1000:1000"
    assert result["image_workdir"] == "/native" and result["reserved_paths_checked"]
    assert "not-for-output" not in str(result)
    assert [call.args[0] for call in container.get_archive.call_args_list] == list(RESERVED_PATHS)
    container.start.assert_not_called()
    container.remove.assert_called_once_with(v=True)
    assert client.containers.create.call_args.kwargs["network_disabled"]


@pytest.mark.parametrize("path", RESERVED_PATHS)
def test_reserved_control_path_collision_is_rejected_without_reading_contents(path):
    client, container, stream = fixture(occupied=path)
    with pytest.raises(HarborConfigurationError, match="reserved"):
        inspect_image(client, IMAGE, limits={"memory_mb": 128, "cpu_cores": 0.25, "disk_mb": 64})
    stream.close.assert_called_once()
    container.start.assert_not_called()
    container.remove.assert_called_once_with(v=True)


def test_floating_image_is_rejected_before_container_creation():
    client, container, _ = fixture()
    with pytest.raises(HarborConfigurationError, match="immutable"):
        inspect_image(client, "floating:latest", limits={})
    client.images.get.assert_not_called()
    client.containers.create.assert_not_called()

"""Inspect administrator-installed native images without running their code."""

from .installation import image_pin
from .native import HarborConfigurationError

RESERVED_PATHS = ("/evalhive-runtime", "/evalhive-transfer", "/evalhive-stage", "/evalhive-model")


def inspect_image(client, reference, *, limits):
    """The caller supplies the EvalHive rootless Docker client, never a worker."""
    from docker.errors import NotFound

    image_pin(reference)
    image = client.images.get(reference)
    image_pin(image.id)
    config = image.attrs.get("Config") or {}
    container = client.containers.create(
        image.id, network_disabled=True, labels={"evalhive.engineering": "harbor-image-inspection"}
    )
    try:
        for path in RESERVED_PATHS:
            try:
                stream, _ = container.get_archive(path)
            except NotFound:
                continue
            if hasattr(stream, "close"):
                stream.close()
            raise HarborConfigurationError("Native image uses a reserved runtime control path")
    finally:
        container.remove(v=True)
    return {
        "image": image.id,
        "image_user": config.get("User") or "",
        "image_workdir": config.get("WorkingDir") or "/",
        "limits": dict(limits),
        "reserved_paths_checked": True,
    }

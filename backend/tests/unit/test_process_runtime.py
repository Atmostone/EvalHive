"""A connector image is a process dependency, not a prompt/model implementation."""

import json
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.config import get_settings
from app.runtime import docker as runtime
from app.runtime.spec import BindMount, NetworkSpec, ProcessSpec, ResourceLimits
from app.runtime.workspace import prepare_layout, write_private_json


@pytest.fixture
def setup_runtime(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "host_data_dir", str(tmp_path / "host"))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "local"))
    monkeypatch.setattr(settings, "runtime_readonly_roots", [str(tmp_path / "tools")])
    client = MagicMock()
    client.info.return_value = {"SecurityOptions": ["name=rootless", "name=seccomp"]}
    client.images.get.return_value = SimpleNamespace(id="sha256:" + "a" * 64)
    client.containers.run.return_value = SimpleNamespace(id="container-id")
    manifest = {"id": "external.fake", "version": "7.0", "image": "registry/fake:7.0"}
    monkeypatch.setattr(
        runtime,
        "get_manifest",
        lambda connector, version: (
            manifest if (connector, version) == ("external.fake", "7.0") else None
        ),
    )
    spec = ProcessSpec(
        task_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        attempt_id=str(uuid.uuid4()),
        connector_id="external.fake",
        connector_version="7.0",
        argv=("custom-process", "--native-flag"),
        environment={"CUSTOM_HARNESS_OPTION": "private-test-value"},
    )
    layout = prepare_layout(spec.task_id, spec.attempt_id)
    spec = replace(spec, mounts=(layout.workspace_mount(),))
    return runtime.DockerProcessRuntime(client), client, spec, layout


def test_registered_image_opaque_process_and_resource_controls(setup_runtime):
    process_runtime, client, spec, layout = setup_runtime
    handle = process_runtime.spawn(spec)
    assert handle.container_id == "container-id" and handle.image_id == "sha256:" + "a" * 64
    client.images.get.assert_called_once_with("registry/fake:7.0")
    args = client.containers.run.call_args.kwargs
    assert args["image"] == handle.image_id
    assert args["entrypoint"] == ["custom-process", "--native-flag"]
    assert args["environment"] == spec.environment
    assert args["volumes"][layout.host_workspace] == {"bind": "/workspace", "mode": "rw"}
    assert args["working_dir"] == "/workspace" and args["cpu_quota"] == 100000
    assert args["mem_limit"] == "2g" and args["cap_drop"] == ["ALL"]
    assert args["security_opt"] == ["no-new-privileges:true"] and args["init"]
    assert args["labels"]["evalhive.attempt_id"] == spec.attempt_id
    assert "private-test-value" not in repr(spec)


def test_rootful_daemon_is_rejected_before_container_creation(setup_runtime):
    process_runtime, client, spec, _ = setup_runtime
    client.info.return_value = {"SecurityOptions": ["name=seccomp"]}
    with pytest.raises(runtime.RuntimeFailure, match="rootless"):
        process_runtime.spawn(spec)
    client.containers.run.assert_not_called()


def test_image_can_be_frozen_before_launch_and_tag_drift_is_rejected(setup_runtime):
    process_runtime, client, spec, _ = setup_runtime
    image_id = process_runtime.resolve_image(spec.connector_id, spec.connector_version)
    pinned = replace(spec, image_id=image_id)
    assert process_runtime.spawn(pinned).image_id == image_id
    client.images.get.return_value = SimpleNamespace(id="sha256:" + "b" * 64)
    client.containers.run.reset_mock()
    with pytest.raises(runtime.RuntimeFailure, match="changed"):
        process_runtime.spawn(pinned)
    client.containers.run.assert_not_called()


def test_missing_registered_image_is_a_preflight_error(setup_runtime):
    process_runtime, client, spec, _ = setup_runtime
    client.images.get.side_effect = runtime.ImageNotFound("not installed")
    with pytest.raises(runtime.RuntimeFailure, match="not installed"):
        process_runtime.spawn(spec)
    client.containers.run.assert_not_called()


@pytest.mark.parametrize(
    "resource",
    [
        ResourceLimits(memory="3g"),
        ResourceLimits(memory="0g"),
        ResourceLimits(memory="1m"),
        ResourceLimits(cpu_quota=200001),
        ResourceLimits(pids=0),
    ],
)
def test_resources_must_fit_server_policy(setup_runtime, resource):
    process_runtime, client, spec, _ = setup_runtime
    with pytest.raises(runtime.RuntimeFailure):
        process_runtime.spawn(replace(spec, resources=resource))
    client.containers.run.assert_not_called()


def test_portal_namespace_is_scoped_to_task_workspace_and_environment_role(setup_runtime):
    process_runtime, client, spec, _ = setup_runtime
    target = SimpleNamespace(
        id="environment-id",
        status="running",
        labels={
            "evalhive.role": "environment",
            "evalhive.workspace_id": spec.workspace_id,
            "evalhive.task_id": spec.task_id,
        },
    )
    client.containers.get.return_value = target
    portal = replace(spec, network=NetworkSpec(container="preprocessor"))
    process_runtime.spawn(portal)
    args = client.containers.run.call_args.kwargs
    assert args["network_mode"] == "container:environment-id" and "network" not in args
    for key, bad in [
        ("evalhive.task_id", "other"),
        ("evalhive.workspace_id", "other"),
        ("evalhive.role", "runner"),
    ]:
        good = target.labels[key]
        target.labels[key] = bad
        with pytest.raises(runtime.RuntimeFailure, match="does not belong"):
            process_runtime.spawn(portal)
        target.labels[key] = good
    target.status = "exited"
    with pytest.raises(runtime.RuntimeFailure, match="not running"):
        process_runtime.spawn(portal)


def test_only_one_registered_network_mode_is_selected(setup_runtime):
    process_runtime, client, spec, _ = setup_runtime
    for network in [NetworkSpec(bridge="host"), NetworkSpec(bridge="one", disabled=True)]:
        with pytest.raises(runtime.RuntimeFailure):
            process_runtime.spawn(replace(spec, network=network))
    process_runtime.spawn(replace(spec, network=NetworkSpec(disabled=True)))
    args = client.containers.run.call_args.kwargs
    assert args["network_disabled"] and "network" not in args


def test_mounts_cannot_escape_the_task_or_mutate_shared_resources(setup_runtime):
    process_runtime, client, spec, layout = setup_runtime
    host_data = get_settings().host_data_dir
    shared = host_data + "/shared/" + spec.workspace_id + "/rules.md"
    process_runtime.spawn(
        replace(spec, mounts=(*spec.mounts, BindMount(shared, "/resources/rules.md")))
    )
    bad_mounts = [
        BindMount("/etc", "/host"),
        BindMount(shared, "/resources/rules.md", False),
        BindMount(layout.host_workspace, "/"),
        BindMount(layout.host_workspace + "/../private", "/other"),
        BindMount(host_data + "/workspaces/" + str(uuid.uuid4()), "/workspace"),
    ]
    for bad in bad_mounts:
        with pytest.raises(runtime.RuntimeFailure):
            process_runtime.spawn(replace(spec, mounts=(bad,)))


def test_stop_and_inspect_do_not_return_private_configuration(setup_runtime):
    process_runtime, client, spec, _ = setup_runtime
    container = MagicMock()
    container.id, container.status = "owned", "exited"
    container.labels = {"evalhive.role": "runner", "evalhive.workspace_id": spec.workspace_id}
    container.attrs = {
        "Image": "sha256:actual",
        "State": {"ExitCode": 0},
        "Config": {"Env": ["PRIVATE=test-value"]},
    }
    client.containers.get.return_value = container
    result = process_runtime.inspect("owned", spec.workspace_id)
    assert result["exit_code"] == 0 and "PRIVATE" not in json.dumps(result)
    assert not process_runtime.stop("owned", "foreign")
    container.stop.assert_not_called()
    assert process_runtime.stop("owned", spec.workspace_id, grace_seconds=2, remove=True)
    container.stop.assert_called_once_with(timeout=2)
    container.remove.assert_called_once_with(force=True)


def test_private_inputs_are_atomic_and_not_inside_artifact_tree(setup_runtime):
    _, _, spec, layout = setup_runtime
    path = write_private_json(layout, "input.json", {"private": "test-value"})
    assert path == layout.host_private + "/input.json"
    assert (layout.private / "input.json").stat().st_mode & 0o777 == 0o600
    assert layout.private.stat().st_mode & 0o777 == 0o700
    assert not list(layout.workspace.iterdir())
    write_private_json(layout, "input.json", {"private": "rotated"})
    assert json.loads((layout.private / "input.json").read_text()) == {"private": "rotated"}
    assert len(list(layout.private.iterdir())) == 1
    with pytest.raises(ValueError):
        write_private_json(layout, "../escape.json", {})
    other = prepare_layout(spec.task_id, str(uuid.uuid4()))
    assert other.workspace != layout.workspace
    assert not list(other.workspace.iterdir())


def test_attempt_layout_rejects_managed_symlinks(setup_runtime):
    _, _, spec, layout = setup_runtime
    attempt_id = str(uuid.uuid4())
    (layout.root.parent / attempt_id).symlink_to(layout.workspace, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_layout(spec.task_id, attempt_id)


def test_environment_helpers_label_the_workspace_for_namespace_validation(
    setup_runtime, monkeypatch
):
    from app.quality import external_eval

    _, client, spec, _ = setup_runtime
    monkeypatch.setattr(external_eval, "_client", lambda: client)
    monkeypatch.setattr(external_eval, "ensure_workspace_dir", lambda _: None)
    monkeypatch.setattr(external_eval, "gym_host_path", lambda: "/registered/gym")
    external_eval.start_preprocess(
        spec.task_id,
        "case",
        "python preprocess.py",
        "test-time",
        keep_alive=True,
        workspace_id=spec.workspace_id,
    )
    labels = client.containers.run.call_args.kwargs["labels"]
    assert labels["evalhive.role"] == "environment"
    assert labels["evalhive.task_id"] == spec.task_id
    assert labels["evalhive.workspace_id"] == spec.workspace_id


def test_attempt_seed_isolated_from_previous_attempts(setup_runtime):
    from app.runtime.workspace import seed_workspace

    _, _, _, layout = setup_runtime
    task_root = layout.root.parent
    (task_root / 'input.txt').write_text('prepared input')
    prior = task_root / str(uuid.uuid4()) / 'workspace'
    prior.mkdir(parents=True)
    (prior.parent / 'private').mkdir()
    (prior / 'answer.txt').write_text('old answer must not leak')
    named_input = task_root / str(uuid.uuid4())
    named_input.mkdir()
    (named_input / 'data.txt').write_text('legitimate UUID input directory')
    seed_workspace(layout)
    assert (layout.workspace / 'input.txt').read_text() == 'prepared input'
    assert not (layout.workspace / prior.parent.name).exists()
    assert (layout.workspace / named_input.name / 'data.txt').read_text() == 'legitimate UUID input directory'
    (layout.workspace / 'input.txt').write_text('attempt mutation')
    assert (task_root / 'input.txt').read_text() == 'prepared input'
    (task_root / 'escape').symlink_to('/outside')
    with pytest.raises(ValueError, match='symlinks'):
        seed_workspace(layout)


def test_environment_image_selection_is_connector_neutral(setup_runtime):
    process_runtime, client, spec, _ = setup_runtime
    manifest = runtime.get_manifest(spec.connector_id, spec.connector_version)
    manifest['requirements'] = {'environment_images': {'fixture-tools': 'registry/fake-tools:7.0'}}
    frozen = process_runtime.resolve_image(spec.connector_id, spec.connector_version, 'fixture-tools')
    handle = process_runtime.spawn(replace(spec, environment_id='fixture-tools', image_id=frozen))
    assert handle.image_id == frozen
    assert client.images.get.call_args.args == ('registry/fake-tools:7.0',)

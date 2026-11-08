"""Administrator parsing preserves supported services and rejects wider authority."""

import pytest
from evalhive_harbor.compose import read_compose
from evalhive_harbor.native import HarborConfigurationError, parse_task
from test_native import task


def native_compose(tmp_path, data):
    root = task(tmp_path)
    (root / "environment/docker-compose.yaml").write_text(data)
    return parse_task(root)


def test_service_start_order_native_argv_and_scoped_storage_are_preserved(tmp_path):
    native = native_compose(
        tmp_path,
        """services:
  main:
    image: ${PREBUILT_IMAGE_NAME}
    depends_on: [database]
    working_dir: /native
    command: 'python -c "two words"'
    environment: {NATIVE_MODE: "fixture", EMPTY: ""}
    volumes: [shared:/workspace]
  database:
    image: native-db:fixture
    command: [server, "two words"]
    user: "1000:1000"
    cpus: "0.25"
    mem_limit: 128m
    volumes: [shared:/data:ro]
volumes:
  shared: {}
""",
    )
    result = read_compose(native)
    assert result["order"] == ["database", "main"]
    assert result["volumes"] == ["shared"]
    assert result["services"]["main"]["command"] == ["python", "-c", "two words"]
    assert result["services"]["main"]["environment"] == {"NATIVE_MODE": "fixture", "EMPTY": ""}
    assert result["services"]["database"]["native_limits"] == {"cpu_cores": 0.25, "memory_mb": 128}
    assert result["services"]["database"]["storage"] == [
        {"volume": "shared", "target": "/data", "read_only": True}
    ]


@pytest.mark.parametrize(
    "data",
    [
        "services: {main: {privileged: true}}",
        "services: {main: {ports: ['8080:80']}}",
        "services: {main: {network_mode: host}}",
        "services: {main: {env_file: /owner/.env}}",
        "services: {main: {environment: {OWNER: '${OWNER_SECRET}'}}}",
        "services: {main: {volumes: ['/host:/data']}}",
        "services: {main: {build: ../outside}}",
        "services: {main: {depends_on: {db: {condition: service_healthy}}}, db: {image: native}}",
        "services: {main: {depends_on: [db]}, db: {depends_on: [main]}}",
        "services: {main: {command: first, command: second}}",
        "services: {main: &shared {image: native}, db: *shared}",
        "services: {main: {image: native}}\nvolumes: {external: {external: true}}",
    ],
)
def test_unsupported_compose_semantics_cannot_silently_expand_runtime_authority(tmp_path, data):
    with pytest.raises(HarborConfigurationError):
        read_compose(native_compose(tmp_path, data))


def test_native_default_main_remains_present_with_extra_services(tmp_path):
    result = read_compose(native_compose(tmp_path, "services: {database: {image: native}}"))
    assert set(result["services"]) == {"main", "database"}


def test_no_compose_source_remains_a_single_native_task(tmp_path):
    assert read_compose(parse_task(task(tmp_path))) is None

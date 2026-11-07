"""Prepared database credentials stay outside the task mounts and public receipts."""

import json

import pytest
from evalhive_mcpmark.world_credentials import configure, main, read


def test_owned_exec_private_input_is_read_from_its_file(monkeypatch, tmp_path, capsys):
    import evalhive_mcpmark.world_credentials as credentials

    data = b'{"service":"postgres","password":"isolated-file-fixture"}'
    path = tmp_path / "input-0"
    path.write_bytes(data)
    path.chmod(0o600)
    monkeypatch.setenv("EVALHIVE_SERVICE_CREDENTIALS_FILE", str(path))
    observed = []
    monkeypatch.setattr(
        credentials, "configure", lambda value: observed.append(value) or {"configured": True}
    )
    main()
    assert observed == [data]
    assert json.loads(capsys.readouterr().out) == {"configured": True}


def test_private_configuration_retries_match_and_receipts_never_contain_the_password(tmp_path):
    root, pg = tmp_path / "control", tmp_path / "postgres-secret"
    value = {"service": "postgres", "password": "isolated-fixture-password"}
    data = json.dumps(value).encode()
    assert (
        configure(data, root=root, pg_secret=pg)
        == configure(data, root=root, pg_secret=pg)
        == {"configured": True}
    )
    assert read(root) == value
    assert (root / "credentials.json").stat().st_mode & 0o777 == 0o600
    assert (pg / "password").read_text() == value["password"]
    with pytest.raises(ValueError, match="frozen"):
        configure(
            json.dumps({**value, "password": "changed-fixture"}).encode(), root=root, pg_secret=pg
        )


def test_private_configuration_rejects_linked_or_shared_control_directory(tmp_path):
    root, pg = tmp_path / "control", tmp_path / "postgres-secret"
    root.mkdir(mode=0o755)
    data = b'{"service":"postgres","password":"isolated-fixture"}'
    with pytest.raises(ValueError, match="private directory"):
        configure(data, root=root, pg_secret=pg)
    root.chmod(0o700)
    pg.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("unchanged")
    (pg / "password").symlink_to(unrelated)
    with pytest.raises(OSError):
        configure(data, root=root, pg_secret=pg)
    assert unrelated.read_text() == "unchanged"

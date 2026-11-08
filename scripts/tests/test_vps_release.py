"""Release activation fails before mutations unless its paired proof is valid."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "vps-release.sh"
SOURCE_SHA, DEPLOYED_SHA = "a" * 40, "b" * 40


@pytest.fixture
def stand(tmp_path):
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    shutil.copyfile(SOURCE, repository / "scripts/vps-release.sh")
    run = tmp_path / "deployment"
    run.mkdir()
    (run / "release.env").write_text("EVALHIVE_RELEASE_SHA=" + DEPLOYED_SHA + "\n")
    (run / "compose.yml").write_text("previous compose\n")
    (run / "compose").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RELEASE_OPERATIONS"\n')
    (run / "compose").chmod(0o755)
    release = run / "releases" / SOURCE_SHA
    release.mkdir(parents=True)
    (release / "release.env").write_text("EVALHIVE_RELEASE_SHA=" + SOURCE_SHA + "\n")
    (release / "compose.yml").write_text("prepared compose\n")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "db.dump").write_bytes(b"synthetic paired database")
    manifest = {
        "complete": True,
        "source": {"compose_project": "evalhive", "platform_git_sha": DEPLOYED_SHA},
        "artifacts": {
            "db.dump": {"sha256": hashlib.sha256((backup / "db.dump").read_bytes()).hexdigest()}
        },
    }
    (backup / "manifest.json").write_text(json.dumps(manifest))
    proof = tmp_path / "restore-proof.json"
    proof.write_text(
        json.dumps(
            {
                "status": "passed",
                "backup_manifest_sha256": hashlib.sha256(
                    (backup / "manifest.json").read_bytes()
                ).hexdigest(),
            }
        )
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, content in {
        "git": f'#!/bin/sh\nif [ "$1" = rev-parse ]; then echo {SOURCE_SHA}; fi\n',
        "docker": '#!/bin/sh\ncase "$1" in info) echo \'["name=rootless"]\';; inspect) echo "${FIXTURE_RUNNING:-false}";; *) exit 99;; esac\n',
    }.items():
        path = tools / name
        path.write_text(content)
        path.chmod(0o755)
    operations = tmp_path / "operations.txt"
    env = {
        "PATH": str(tools) + os.pathsep + os.defpath,
        "HOME": str(tmp_path),
        "EVALHIVE_DEPLOY_DIR": str(run),
        "RELEASE_OPERATIONS": str(operations),
    }
    return repository, run, release, backup, proof, operations, env


def activate(stand):
    repository, _, _, backup, proof, _, env = stand
    return subprocess.run(
        [
            "bash",
            str(repository / "scripts/vps-release.sh"),
            "--activate",
            "--backup",
            str(backup),
            "--restore-proof",
            str(proof),
        ],
        env=env,
        capture_output=True,
        text=True,
    )


def test_activation_preserves_previous_configuration_and_leaves_scheduler_paused(stand):
    _, run, release, _, _, operations, _ = stand
    result = activate(stand)
    assert result.returncode == 0, result.stderr
    assert (run / "compose.yml").read_text() == "prepared compose\n"
    assert (release / "previous-config/compose.yml").read_text() == "previous compose\n"
    assert (
        release / "previous-config/release.env"
    ).read_text() == "EVALHIVE_RELEASE_SHA=" + DEPLOYED_SHA + "\n"
    assert operations.read_text().strip() == "up -d --no-build api orchestrator frontend"


@pytest.mark.parametrize(
    "failure", ["unverified_restore", "corrupt_dump", "wrong_source", "running_writers"]
)
def test_invalid_release_evidence_never_switches_configuration(stand, failure):
    _, run, release, backup, proof, operations, env = stand
    if failure == "unverified_restore":
        value = json.loads(proof.read_text())
        value["status"] = "failed"
        proof.write_text(json.dumps(value))
    elif failure == "corrupt_dump":
        (backup / "db.dump").write_bytes(b"changed database bytes")
    elif failure == "wrong_source":
        (run / "release.env").write_text("EVALHIVE_RELEASE_SHA=" + "c" * 40 + "\n")
    else:
        env["FIXTURE_RUNNING"] = "true"
    result = activate(stand)
    assert result.returncode != 0
    assert (run / "compose.yml").read_text() == "previous compose\n"
    assert not (release / "previous-config").exists()
    assert not operations.exists()

"""Deployment toggle and rollback behavior with fake Docker/health commands only."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
OLD_TAG = "0123456789ab"
NEW_TAG = "abcdef012345"

DOCKER = """#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
root = Path(os.environ['FAKE_DEPLOY_ROOT'])
args = sys.argv[1:]
with (root / 'commands.jsonl').open('a') as f:
    f.write(json.dumps(args) + '\\n')
if args[0] == 'login':
    sys.stdin.read()
elif args[0] == 'compose':
    failure = os.environ.get('FAIL_AT')
    if 'ps' in args and os.environ.get('HAS_POSTGRES'):
        print('fixture-postgres-id')
    if 'pg_dump' in args:
        if failure == 'backup':
            sys.exit(1)
        sys.stdout.buffer.write(b'fixture-database-recovery-point')
    if 'pull' in args and failure == 'pull':
        sys.exit(1)
    if 'migrate' in args and failure == 'migration':
        sys.exit(1)
    if 'up' in args:
        count_file = root / 'up_count'
        count = int(count_file.read_text()) + 1 if count_file.exists() else 1
        count_file.write_text(str(count))
        shutil.copyfile(root / 'compose.vars', root / ('up_' + str(count) + '.vars'))
        if failure == 'update' and count == 1:
            sys.exit(1)
elif args[:2] == ['image', 'ls']:
    print('0123456789ab 2026-10-28 00:00:00')
elif args[:2] == ['image', 'inspect']:
    print(os.environ.get('IMAGE_REVISION', ''))
"""
CURL = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
root = Path(os.environ['FAKE_DEPLOY_ROOT'])
count = int((root / 'up_count').read_text())
failure = os.environ.get('FAIL_AT')
sys.exit(1 if failure == 'rollback_health' or failure == 'health' and count == 1 else 0)
"""


@dataclass
class Deployment:
    root: Path
    script: Path
    env: dict[str, str]
    original: str

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - controlled script and fake subprocess environment
            ["/bin/bash", str(self.script), *args],
            input="fixture-user\nfixture-registry-secret\nfixture-model-secret\nfixture-search-secret\n",
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, script in {
        "docker": DOCKER,
        "curl": CURL,
        "sleep": "#!/bin/sh\nexit 0\n",
        "flock": "#!/bin/sh\nexit 0\n",
    }.items():
        path = commands / name
        path.write_text(script)
        path.chmod(0o700)
    script = tmp_path / "deploy.sh"
    source = (REPO / "ops/deploy/remote-deploy.sh").read_text()
    script.write_text(source.replace("cd /srv/botai", "cd " + shlex.quote(str(tmp_path))))
    original = f"IMAGE_TAG={OLD_TAG}\nPOSTGRES_PASSWORD=fixture-db-secret\n"
    (tmp_path / "compose.vars").write_text(original)
    (tmp_path / "app.secrets").write_text("BOTAI_KEK=fixture-key\n")
    env = {
        **os.environ,
        "FAKE_DEPLOY_ROOT": str(tmp_path),
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
    }
    return Deployment(tmp_path, script, env, original)


@pytest.mark.parametrize(
    ("previous", "mode", "expected"),
    [
        (None, "keep", "false"),
        ("false", "keep", "false"),
        ("true", "keep", "true"),
        ("false", "on", "true"),
        ("true", "off", "false"),
    ],
)
def test_toggle_preserves_or_changes_server_mode(
    deployment: Deployment, previous: str | None, mode: str, expected: str
) -> None:
    if previous is not None:
        (deployment.root / "compose.vars").write_text(
            deployment.original + f"BOTAI_DIAGNOSTICS={previous}\n"
        )
    result = deployment.run(NEW_TAG, mode)
    assert result.returncode == 0, result.stderr
    variables = (deployment.root / "compose.vars").read_text()
    assert f"IMAGE_TAG={NEW_TAG}\n" in variables
    assert f"BOTAI_DIAGNOSTICS={expected}\n" in variables
    assert "POSTGRES_PASSWORD=fixture-db-secret\n" in variables
    assert "fixture-registry-secret" not in result.stdout + result.stderr
    assert "fixture-model-secret" not in result.stdout + result.stderr
    assert "fixture-search-secret" not in result.stdout + result.stderr
    assert not list(deployment.root.glob(".compose.vars.rollback.*"))


def test_search_key_is_delivered_without_overwriting_vault_or_duplicate_keys(
    deployment: Deployment,
) -> None:
    secrets = deployment.root / "app.secrets"
    secrets.write_text("BOTAI_KEK=fixture-key\nEXA_API_KEY=old-search-secret\n")
    result = deployment.run(NEW_TAG)
    assert result.returncode == 0, result.stderr
    assert secrets.read_text() == (
        "BOTAI_KEK=fixture-key\nDEEPSEEK_API_KEY=fixture-model-secret\n"
        "EXA_API_KEY=fixture-search-secret\n"
    )
    assert secrets.stat().st_mode & 0o777 == 0o600
    assert "fixture-search-secret" not in result.stdout + result.stderr


def test_image_revision_must_match_checked_source_before_migration(deployment: Deployment) -> None:
    revision = NEW_TAG + "0" * 28
    deployment.env["IMAGE_REVISION"] = "f" * 40
    result = deployment.run(NEW_TAG, "keep", revision)
    assert result.returncode == 1
    assert "image revision mismatch" in result.stderr
    commands = (deployment.root / "commands.jsonl").read_text()
    assert '"migrate"' not in commands
    assert (deployment.root / "compose.vars").read_text() == deployment.original


def test_database_recovery_point_is_encrypted_and_key_is_separate(deployment: Deployment) -> None:
    deployment.env["HAS_POSTGRES"] = "1"
    result = deployment.run(NEW_TAG)
    assert result.returncode == 0, result.stderr
    backup = next((deployment.root / "backups").glob("*.dump.enc"))
    key = next((deployment.root / ".rollback-keys").glob("*.key"))
    assert len(key.read_text().strip()) == 64
    assert b"fixture-database-recovery-point" not in backup.read_bytes()
    openssl = shutil.which("openssl")
    assert openssl is not None
    restored = subprocess.run(  # noqa: S603 - fixed executable and test-generated paths
        [
            openssl,
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            f"file:{key}",
            "-in",
            str(backup),
        ],
        capture_output=True,
        check=True,
    )
    assert restored.stdout == b"fixture-database-recovery-point"
    assert key.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600


def test_failed_database_backup_blocks_migration(deployment: Deployment) -> None:
    deployment.env.update(HAS_POSTGRES="1", FAIL_AT="backup")
    result = deployment.run(NEW_TAG)
    assert result.returncode == 1
    assert "database backup" in result.stderr
    assert '"migrate"' not in (deployment.root / "commands.jsonl").read_text()
    assert (deployment.root / "compose.vars").read_text() == deployment.original


def test_omitted_mode_defaults_to_keep(deployment: Deployment) -> None:
    (deployment.root / "compose.vars").write_text(deployment.original + "BOTAI_DIAGNOSTICS=true\n")
    result = deployment.run(NEW_TAG)
    assert result.returncode == 0, result.stderr
    assert "BOTAI_DIAGNOSTICS=true\n" in (deployment.root / "compose.vars").read_text()


@pytest.mark.parametrize("mode", ["debug", "true", "on;echo injected"])
def test_invalid_mode_is_rejected_before_credentials_or_docker(
    deployment: Deployment, mode: str
) -> None:
    result = deployment.run(NEW_TAG, mode)
    assert result.returncode == 64
    assert (deployment.root / "compose.vars").read_text() == deployment.original
    assert not (deployment.root / "commands.jsonl").exists()
    assert (deployment.root / "app.secrets").read_text() == "BOTAI_KEK=fixture-key\n"


@pytest.mark.parametrize("failure", ["pull", "migration", "update", "health"])
@pytest.mark.parametrize("previous", [None, "true", "false"])
def test_failed_deploy_restores_image_and_exact_previous_toggle(
    deployment: Deployment, failure: str, previous: str | None
) -> None:
    original = deployment.original + (f"BOTAI_DIAGNOSTICS={previous}\n" if previous else "")
    (deployment.root / "compose.vars").write_text(original)
    deployment.env["FAIL_AT"] = failure
    result = deployment.run(NEW_TAG, "off" if previous == "true" else "on")
    assert result.returncode == 1, result.stderr
    assert (deployment.root / "compose.vars").read_text() == original
    if failure in {"update", "health"}:
        assert (deployment.root / "up_2.vars").read_text() == original
    else:
        assert not (deployment.root / "up_count").exists()
    assert not list(deployment.root.glob(".compose.vars.rollback.*"))


def test_failed_rollback_is_reported_without_hiding_failure(deployment: Deployment) -> None:
    deployment.env["FAIL_AT"] = "rollback_health"
    result = deployment.run(NEW_TAG, "on")
    assert result.returncode == 1
    assert (deployment.root / "compose.vars").read_text() == deployment.original
    assert "rollback health check failed; operator action required" in result.stderr


def test_invalid_stored_mode_is_not_silently_overwritten(deployment: Deployment) -> None:
    original = deployment.original + "BOTAI_DIAGNOSTICS=unexpected\n"
    (deployment.root / "compose.vars").write_text(original)
    result = deployment.run(NEW_TAG, "on")
    assert result.returncode == 64
    assert (deployment.root / "compose.vars").read_text() == original
    assert not (deployment.root / "commands.jsonl").exists()

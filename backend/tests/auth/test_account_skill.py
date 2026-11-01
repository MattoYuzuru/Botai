"""Local operator skill: private issuance, safe retries and normal BotAI login."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from sqlalchemy import func, select

from botai.auth import security
from botai.db.models import AuthSession, ChatThread, Invite, Source, Space, UserMemory
from tests.auth.support import PASSWORD, Sm, make_user

SCRIPTS = Path(__file__).resolve().parents[3] / ".agents/skills/botai-accounts/scripts"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"account_skill_{name}", SCRIPTS / f"{name}.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments(**values: object) -> Namespace:
    return Namespace(
        **{
            "operation": "create",
            "name": "Саша",
            "email": None,
            "request_id": "friend-sasha",
            "host": "mnema",
            "container": "botai-api-1",
            "base_url": "https://botai.mnema.app",
            **values,
        }
    )


def test_private_credentials_survive_retry_and_reject_different_recipient(tmp_path: Path) -> None:
    broker = load("accounts")
    path, first = broker.prepare(arguments(), tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert first["email"].endswith("@example.com")
    assert len(first["password"]) >= 24
    assert broker.prepare(arguments(), tmp_path) == (path, first)
    for changed in ({"name": "Другой"}, {"email": "other@example.com"}, {"host": "other"}):
        with pytest.raises(ValueError, match="request-id"):
            broker.prepare(arguments(**changed), tmp_path)
    assert json.loads(path.read_text()) == first


def test_access_file_is_not_followed_or_read_if_public(tmp_path: Path) -> None:
    broker = load("accounts")
    path, _ = broker.prepare(arguments(), tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        broker.prepare(arguments(), tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="private"):
        broker.prepare(arguments(), tmp_path)


def test_invite_and_credentials_go_over_stdin_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = load("accounts")
    args = arguments(operation="invite")
    _, data = broker.prepare(args, tmp_path)
    assert data["invite_code"] in data["registration_url"]
    assert "password" not in data

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert data["invite_code"] not in " ".join(argv)
        assert data["invite_code"] in str(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, '{"status":"ready"}', "")

    monkeypatch.setattr(broker.subprocess, "run", run)
    assert broker.remote(args, data) == {"status": "ready"}


def test_untrusted_remote_errors_never_reach_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = load("accounts")
    secret = "synthetic-password-do-not-echo"
    monkeypatch.setattr("sys.argv", ["accounts.py", "list"])
    monkeypatch.setattr(
        broker.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", secret)
    )
    assert broker.main() == 1
    assert secret not in capsys.readouterr().out


@pytest.fixture
def worker(tx_sessionmaker: Sm, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load("remote")
    monkeypatch.setattr(module, "get_sessionmaker", lambda: tx_sessionmaker)
    return module


def request(**values: object) -> dict[str, object]:
    return {
        "operation": "create",
        "name": "Саша",
        "email": "friend@example.com",
        "password": PASSWORD,
        "invite_code": "synthetic-invite-123456",
        **values,
    }


async def test_created_account_is_empty_login_works_and_retry_is_idempotent(
    worker: ModuleType, tx_sessionmaker: Sm, client: httpx.AsyncClient
) -> None:
    await make_user(tx_sessionmaker, is_owner=True)
    result = await worker.operate(request())
    assert result["status"] == "created"
    assert result["is_owner"] is False
    retry = await worker.operate(request())
    assert retry["status"] == "existing"
    assert retry["id"] == result["id"]
    async with tx_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Invite)) == 1
        for model in (Space, Source, ChatThread, UserMemory, AuthSession):
            assert (
                await db.scalar(
                    select(func.count()).select_from(model).where(model.user_id == result["id"])
                )
                == 0
            )
    login = await client.post(
        "/api/v1/auth/login", json={"email": "friend@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["user"]["id"] == result["id"]
    assert PASSWORD not in json.dumps(result)


async def test_existing_account_password_and_owner_sessions_are_preserved(
    worker: ModuleType, tx_sessionmaker: Sm, client: httpx.AsyncClient
) -> None:
    await make_user(tx_sessionmaker, "owner@example.com", is_owner=True, password=PASSWORD)
    await client.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
    )
    await worker.operate(request())
    with pytest.raises(worker.OperationError, match="account_conflict"):
        await worker.operate(request(password="different-password"))
    assert (await client.get("/api/v1/me")).json()["user"]["email"] == "owner@example.com"
    assert (await worker.operate(request()))["status"] == "existing"


async def test_invite_retry_does_not_extend_expiry_and_link_registers_once(
    worker: ModuleType, tx_sessionmaker: Sm, client: httpx.AsyncClient
) -> None:
    await make_user(tx_sessionmaker, is_owner=True)
    result = await worker.operate(request(operation="invite"))
    assert await worker.operate(request(operation="invite")) == result
    body = {
        "inviteCode": request()["invite_code"],
        "email": "new@example.com",
        "displayName": "Друг",
        "password": PASSWORD,
    }
    assert (await client.post("/api/v1/auth/register", json=body)).status_code == 201
    with pytest.raises(worker.OperationError, match="used_or_expired"):
        await worker.operate(request(operation="invite"))


async def test_expired_invite_does_not_get_renewed(worker: ModuleType, tx_sessionmaker: Sm) -> None:
    owner_id = await make_user(tx_sessionmaker, is_owner=True)
    async with tx_sessionmaker() as db:
        db.add(
            Invite(
                code_hash=security.token_hash(str(request()["invite_code"])),
                created_by=owner_id,
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await db.commit()
    with pytest.raises(worker.OperationError, match="used_or_expired"):
        await worker.operate(request(operation="invite"))


async def test_list_omits_hashes_and_primary_secrets(
    worker: ModuleType, tx_sessionmaker: Sm
) -> None:
    await make_user(tx_sessionmaker, is_owner=True, password=PASSWORD)
    result = await worker.operate({"operation": "list"})
    assert len(result["users"]) == 1
    assert PASSWORD not in json.dumps(result)
    assert "hash" not in json.dumps(result)

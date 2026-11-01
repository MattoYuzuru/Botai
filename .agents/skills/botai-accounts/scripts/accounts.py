"""Local account broker: secrets go to stdin and a private file, never stdout or argv."""

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit

STATE_ROOT = Path.home() / ".local/state/botai/accounts"
ERRORS = {
    "account_conflict": "Аккаунт уже существует с другим именем/паролем или недоступен.",
    "no_active_owner": "В окружении нет активного владельца.",
    "invite_used_or_expired": "Приглашение уже использовано или истекло.",
    "invalid_operation": "Операция не поддерживается.",
    "operation_failed": "Операция на сервере не подтверждена.",
}


class RemoteError(RuntimeError):
    """Contains only a fixed, non-secret message from ERRORS."""


def private_write(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def prepare(args, state_root=STATE_ROOT):
    """Persist credentials before SSH, so an interrupted request can be retried safely."""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", args.request_id):
        raise ValueError("request-id: use 1–80 letters, digits, underscores or hyphens")
    directory = state_root / args.request_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("State directory must not be a symlink")
    os.chmod(directory, 0o700)
    path = directory / "access.json"
    target = {"host": args.host, "container": args.container, "base_url": args.base_url}
    if path.exists() or path.is_symlink():
        if (
            path.is_symlink()
            or not stat.S_ISREG(path.stat().st_mode)
            or path.stat().st_mode & 0o077
        ):
            raise ValueError("Access file must be a regular private file (0600)")
        data = json.loads(path.read_text())
        if data["operation"] != args.operation or data["target"] != target:
            raise ValueError("request-id already belongs to another operation or target")
        if args.operation == "create" and (
            data["name"] != args.name.strip()
            or (args.email and data["email"] != args.email.strip().lower())
        ):
            raise ValueError("request-id already belongs to another name or email")
        return path, data
    data = {"operation": args.operation, "target": target, "invite_code": secrets.token_urlsafe(24)}
    if args.operation == "create":
        data.update(
            name=args.name.strip(),
            email=(
                args.email.strip().lower()
                if args.email
                else f"botai-test-{secrets.token_hex(8)}@example.com"
            ),
            password=secrets.token_urlsafe(24),
            login_url=f"{args.base_url}/login",
        )
        if not data["name"] or len(data["name"]) > 120:
            raise ValueError("Name must contain 1–120 characters")
    else:
        data["registration_url"] = f"{args.base_url}/register?invite={quote(data['invite_code'])}"
    private_write(path, data)
    return path, data


def remote(args, data):
    source = Path(__file__).with_name("remote.py").read_text()
    payload = {
        key: value
        for key, value in data.items()
        if key not in {"target", "login_url", "registration_url"}
    }
    source += f"\nimport asyncio\nasyncio.run(entry(json.loads({json.dumps(payload)!r})))\n"
    ssh = shutil.which("ssh")
    if ssh is None:
        raise RuntimeError("SSH is unavailable")
    result = subprocess.run(  # noqa: S603 - host/container are validated; secrets use stdin only
        [
            ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-T",
            args.host,
            f"sudo -n docker exec -i {args.container} python -",
        ],
        input=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    try:
        response = json.loads(result.stdout)
    except ValueError:
        response = {}
    if response.get("error") in ERRORS:
        raise RemoteError(ERRORS[response["error"]])
    if result.returncode or not response or "error" in response:
        # SSH/container errors and tracebacks may contain SQL parameters. Keep them private.
        raise RuntimeError("SSH/container operation failed. Retry with the same request-id.")
    return response


def parser():
    p = argparse.ArgumentParser(description="BotAI: создать аккаунт или приглашение")
    p.add_argument("operation", choices=["create", "invite", "list"])
    p.add_argument("--name", default="Тестовый аккаунт")
    p.add_argument("--email")
    p.add_argument("--request-id")
    p.add_argument("--host", default="mnema")
    p.add_argument("--container", default="botai-api-1")
    p.add_argument("--base-url", default="https://botai.mnema.app")
    return p


def main():
    p = parser()
    args = p.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", args.host):
        p.error("invalid SSH alias")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", args.container):
        p.error("invalid container name")
    parsed = urlsplit(args.base_url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        p.error("base-url must be an HTTP(S) origin without credentials")
    args.base_url = args.base_url.rstrip("/")
    path = None
    try:
        if args.operation == "list":
            data = {"operation": "list"}
        else:
            if not args.request_id:
                p.error("--request-id is required for create/invite")
            path, data = prepare(args)
        result = remote(args, data)
        if path:
            result["access_file"] = str(path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        # Do not interpolate exceptions: they can include credentials from a broken response.
        if isinstance(exc, RemoteError):
            print(str(exc))
        else:
            print(
                "Операция не подтверждена. Проверьте доступ/параметры; повторите тот же request-id."
            )
        if path:
            print(f"Закрытый файл для повтора: {path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

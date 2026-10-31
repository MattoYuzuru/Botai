"""Secret broker of the live benchmark (v4): primary credentials never reach the agent.

The owner's primary secrets (HSE login and password) are stored **only** in the macOS
Keychain. Values are read into the memory of a consuming process and are never printed,
logged, persisted in artifacts, or passed on a command line. The only public surface is:

* :func:`get_secret` -- the consumer API used by ``bench/live`` (returns ``str`` in memory);
* CLI ``migrate`` / ``status`` / ``usage`` / ``purge`` -- safe to run from an agent shell:
  they print service names, booleans and file references, never values.

Keychain layout: account ``botai-v4-bench``, services ``botai.live.hse.mail`` and
``botai.live.hse.password``. ``security add-generic-password`` is fed the value through
stdin (twice: value + retype), so the secret never appears in the process argv.

Central University: the saved storage state of the ``central-university-lms`` skill is
not a primary secret but a session; it is also handled here so that a consumer has one
entry point. The state file is *not* copied into this repository.

    python -m bench.live.secret_broker status
    python -m bench.live.secret_broker migrate
    python -m bench.live.secret_broker usage
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

KEYCHAIN_ACCOUNT = "botai-v4-bench"
SERVICE_PREFIX = "botai.live"

#: broker name -> Keychain service. The names are public; values are not.
SECRETS: dict[str, str] = {
    "hse_mail": f"{SERVICE_PREFIX}.hse.mail",
    "hse_password": f"{SERVICE_PREFIX}.hse.password",
}

#: `.env` variable -> broker name. Used by one-time migration only.
ENV_NAMES: dict[str, str] = {
    "HSE_MAIL": "hse_mail",
    "HSE_PASSWORD": "hse_password",
}

ENV_COMMENT = """\
# HSE_MAIL / HSE_PASSWORD перенесены в macOS Keychain 2026-10-31 (public repo, `.env`
# больше не содержит первичных секретов). Сервисы: botai.live.hse.mail,
# botai.live.hse.password; выдаёт только код через bench/live/secret_broker.py.
# Значения не печатать и не возвращать обратно в `.env`.
"""

CU_SKILL_STATE = Path(
    "~/.config/yuzuru-codex-skills/central-university-lms/storage-state.json"
).expanduser()

_SECURITY = "/usr/bin/security"


class SecretError(RuntimeError):
    """Broker failure that is safe to show to a human: never contains a value."""


class SecretUnavailable(SecretError):
    """The secret is neither in the Keychain nor in the source that migration reads."""


# --- Keychain primitives -------------------------------------------------------------------------


def _run_security(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_SECURITY, *args],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def has_secret(name: str) -> bool:
    """True when the Keychain item exists; the value is not read."""
    service = SECRETS.get(name)
    if service is None:
        raise SecretError(f"unknown secret name {name!r}")
    proc = _run_security(["find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service])
    return proc.returncode == 0


def get_secret(name: str) -> str:
    """Return the secret value in memory. Consumers must never print or persist it."""
    service = SECRETS.get(name)
    if service is None:
        raise SecretError(f"unknown secret name {name!r}")
    proc = _run_security(["find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"])
    if proc.returncode != 0:
        raise SecretUnavailable(
            f"{name}: not found in the Keychain (service {service}); run `migrate` first"
        )
    value = proc.stdout.rstrip("\n")
    if not value:
        raise SecretUnavailable(f"{name}: Keychain item {service} is empty")
    return value


def store_secret(name: str, value: str) -> None:
    """Store one secret and verify it. The value is passed through stdin, not argv."""
    service = SECRETS.get(name)
    if service is None:
        raise SecretError(f"unknown secret name {name!r}")
    if not value:
        raise SecretError(f"{name}: refusing to store an empty value")
    # `-w` without a value makes `security` read the password and its retype from stdin.
    proc = _run_security(
        ["add-generic-password", "-U", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"],
        stdin=f"{value}\n{value}\n",
    )
    if proc.returncode != 0:
        raise SecretError(f"{name}: `security add-generic-password` failed ({proc.returncode})")
    if get_secret(name) != value:
        raise SecretError(f"{name}: Keychain verification after write failed")
    # The value stays in this process only; drop the reference explicitly.
    del value


def delete_secret(name: str) -> bool:
    service = SECRETS.get(name)
    if service is None:
        raise SecretError(f"unknown secret name {name!r}")
    proc = _run_security(["delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service])
    return proc.returncode == 0


# --- one-time migration from .env ----------------------------------------------------------------


def _read_env_values(path: Path) -> dict[str, str]:
    """Read only the credential variables from `.env`. Values stay in memory."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        if key not in ENV_NAMES:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            values[key] = value
    return values


def _scrub_env(path: Path, *, expected_absent: list[str]) -> bool:
    """Remove migrated variables from `.env`, keeping everything else intact."""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in ENV_NAMES and not line.lstrip().startswith("#"):
            removed.append(key)
            continue
        kept.append(line)
    if not removed:
        return False
    old_mode = path.stat().st_mode & 0o777
    body = "\n".join(kept).rstrip("\n")
    if body:
        body += "\n"
    body += "\n" + ENV_COMMENT
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(tmp, old_mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # Verify that no migrated value survived anywhere in the file (values in memory only).
    text = path.read_text(encoding="utf-8")
    for value in expected_absent:
        if value and value in text:
            raise SecretError("migration: a migrated value is still present in .env")
    return True


def migrate(env_path: Path = ENV_PATH, *, force: bool = False) -> dict[str, Any]:
    """Move HSE credentials from `.env` into the Keychain, then scrub `.env`."""
    env_values = _read_env_values(env_path)
    report: dict[str, Any] = {
        "services": {name: SECRETS[name] for name in SECRETS},
        "stored": [],
        "skippedExisting": [],
        "envScrubbed": False,
        "envPath": str(env_path),
    }
    for env_name, name in ENV_NAMES.items():
        value = env_values.get(env_name)
        present = has_secret(name)
        if value is not None:
            if present and not force:
                report["skippedExisting"].append(name)
            else:
                store_secret(name, value)
                report["stored"].append(name)
        elif not present:
            raise SecretUnavailable(
                f"{env_name}: not in {env_path.name} and not in the Keychain; nothing to migrate"
            )
    missing = [name for name in SECRETS if not has_secret(name)]
    if missing:
        raise SecretError(f"migration: Keychain still lacks {', '.join(missing)}")
    report["envScrubbed"] = _scrub_env(
        env_path, expected_absent=[v for v in env_values.values() if v]
    )
    return report


# --- Central University session source -----------------------------------------------------------


def cu_state_path() -> Path:
    """Path of the CU skill storage state, outside the repository."""
    override = os.environ.get("BOTAI_CU_STATE")
    return Path(override).expanduser() if override else CU_SKILL_STATE


def cu_state_status() -> dict[str, Any]:
    """Describe the CU session source without reading its content into the output."""
    path = cu_state_path()
    status: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if path.exists():
        mode = path.stat().st_mode & 0o777
        status["mode"] = oct(mode)
        status["private"] = mode & 0o077 == 0
        status["bytes"] = path.stat().st_size
        status["mtime"] = path.stat().st_mtime
    return status


def read_cu_storage_state() -> dict[str, Any]:
    """Return the saved CU storage state to a consumer. Never print the result."""
    path = cu_state_path()
    if not path.exists():
        raise SecretUnavailable(f"CU storage state not found at {path}; run the skill login")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "cookies" not in data:
        raise SecretUnavailable("CU storage state does not look like a Playwright storage state")
    return data


# --- CLI ------------------------------------------------------------------------------------------


def _cmd_status(_: argparse.Namespace) -> int:
    payload = {
        "keychainAccount": KEYCHAIN_ACCOUNT,
        "secrets": {
            name: {"service": service, "present": has_secret(name)}
            for name, service in SECRETS.items()
        },
        "envFile": {
            "path": str(ENV_PATH),
            "exists": ENV_PATH.exists(),
            "hasCredentialVariables": bool(_read_env_values(ENV_PATH)),
        },
        "cuSession": cu_state_status(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(item["present"] for item in payload["secrets"].values()) else 1


def _cmd_migrate(args: argparse.Namespace) -> int:
    report = migrate(Path(args.env).expanduser() if args.env else ENV_PATH, force=args.force)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    removed = [name for name in SECRETS if delete_secret(name)]
    print(json.dumps({"removed": removed}, ensure_ascii=False))
    return 0


def _iter_text_files(root: Path):
    skip_dirs = {
        ".git",
        "node_modules",
        ".venv",
        "dist",
        "__pycache__",
        ".ruff_cache",
        ".pytest_cache",
    }
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix in {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
                ".ico",
                ".woff",
                ".woff2",
                ".ttf",
                ".pdf",
                ".zip",
                ".har",
            }:
                continue
            if path.stat().st_size > 512_000:
                continue
            yield path


def _cmd_usage(_: argparse.Namespace) -> int:
    """Report code references to the migrated variables (the app must not need them)."""
    references: list[dict[str, Any]] = []
    for path in _iter_text_files(REPO_ROOT):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for left, needle in ((path, "HSE_MAIL"), (path, "HSE_PASSWORD")):
            if needle not in text:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if needle in line:
                    references.append(
                        {
                            "file": str(left.relative_to(REPO_ROOT)),
                            "line": lineno,
                            "text": line.strip()[:160],
                        }
                    )
    env_has_values = bool(_read_env_values(ENV_PATH))
    print(
        json.dumps(
            {
                "references": references,
                "referenceCount": len(references),
                "envStillHasValues": env_has_values,
                "verdict": "no application/test code reads the migrated variables"
                if not references
                else "code references the migrated variables: review",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not references and not env_has_values else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bench.live.secret_broker",
        description="Keychain secret broker of the live benchmark. Values are never printed.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="which secrets exist (booleans only)")
    status.set_defaults(func=_cmd_status)
    migrate_cmd = sub.add_parser("migrate", help="move HSE credentials from .env to the Keychain")
    migrate_cmd.add_argument(
        "--env", default=None, help="path to the env file (default: repo .env)"
    )
    migrate_cmd.add_argument(
        "--force", action="store_true", help="overwrite existing Keychain items"
    )
    migrate_cmd.set_defaults(func=_cmd_migrate)
    purge = sub.add_parser("purge", help="delete broker items from the Keychain")
    purge.set_defaults(func=_cmd_purge)
    usage = sub.add_parser("usage", help="grep the repository for migrated variable names")
    usage.set_defaults(func=_cmd_usage)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SecretError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())

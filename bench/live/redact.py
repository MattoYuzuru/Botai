"""Redaction for live-benchmark artifacts: primary secrets never reach a file or the chat.

The public repository must not contain passwords, login e-mails, cookies, tokens or
personal data. Raw session material lives only under
``~/.local/state/botai-v4-bench/`` (outside the repository, mode 0700); even there the
**primary** secrets are removed, because the owner's rule is "password fields are scrubbed
from HAR and logs".

Three layers are used together:

* :class:`Redactor` -- exact-value replacement of every registered primary secret;
* :func:`clean_url` / :func:`endpoint_template` -- drop sensitive query parameters and
  stable ids from URLs;
* :func:`sanitize_har` -- strip cookies, auth headers, password fields and response
  bodies from a Playwright HAR file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: Headers whose values are session material and never need to be kept.
SENSITIVE_HEADERS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-session-id",
        "x-sessionid",
        "x-requested-with-token",
        "referer",  # may carry a signed query; the endpoint itself is enough
    }
)

#: Query parameters that carry credentials, one-time codes or session identifiers.
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "code",
        "code_challenge",
        "code_verifier",
        "state",
        "session_code",
        "session_state",
        "sessionid",
        "session_id",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "ticket",
        "execution",
        "password",
        "passwd",
        "pwd",
        "secret",
        "sig",
        "signature",
        "key",
        "auth",
        "nonce",
        "login_hint",
        "email",
        "user",
        "username",
    }
)

#: Field names of a login form; their values are always replaced.
PASSWORD_FIELDS = frozenset({"password", "passwd", "pwd", "pass", "credential", "secret"})
USERNAME_FIELDS = frozenset({"username", "user", "login", "email", "identifier", "credentialid"})

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{24,}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
_BEARER = re.compile(r"(?i)\b(bearer|token|jwt)\s+[A-Za-z0-9._~+/=-]{12,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")
_REDACTED = "<redacted>"


class Redactor:
    """Exact-value redaction for every primary secret a run knows about.

    Consumers register values via :meth:`register` right after the broker hands them over;
    nothing else in the benchmark stores them. ``redact`` is applied to console lines,
    form field names, DOM excerpts and every JSON written to disk.
    """

    def __init__(self) -> None:
        self._values: list[str] = []

    def register(self, value: str) -> None:
        if value and value not in self._values:
            self._values.append(value)
        # Longer values first, so an overlapping shorter value cannot mask the longer one.
        self._values.sort(key=len, reverse=True)

    @property
    def registeredCount(self) -> int:
        """How many primary values are known (their content is never exposed)."""
        return len(self._values)

    def contains_secret(self, text: str) -> bool:
        return any(value in text for value in self._values)

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value in self._values:
            text = text.replace(value, _REDACTED)
        text = _JWT.sub("<redacted-jwt>", text)
        text = _BEARER.sub(lambda m: f"{m.group(1)} <redacted>", text)
        text = _EMAIL.sub("<redacted-email>", text)
        return text

    def redact_payload(self, payload: Any) -> Any:
        if isinstance(payload, str):
            return self.redact(payload)
        if isinstance(payload, list):
            return [self.redact_payload(item) for item in payload]
        if isinstance(payload, dict):
            return {str(key): self.redact_payload(value) for key, value in payload.items()}
        return payload


# --- URL handling --------------------------------------------------------------------------------


def clean_url(url: str, *, redactor: Redactor | None = None, keep_query: bool = True) -> str:
    """Redact sensitive query values and long ids; optionally drop the whole query."""
    scheme, _, rest = url.partition("://")
    if not rest:
        return url
    host_path, _, query = rest.partition("?")
    host_path = _UUID.sub("<id>", _LONG_HEX.sub("<id>", host_path))
    if not keep_query:
        query = ""
    if query:
        parts: list[str] = []
        for chunk in query.split("&"):
            key, sep, value = chunk.partition("=")
            lowered = key.lower()
            if lowered in SENSITIVE_QUERY_KEYS or (len(value) >= 24 and "=" not in value):
                parts.append(f"{key}{sep}<redacted>")
            else:
                parts.append(chunk)
        query = "&".join(parts)
    result = f"{scheme}://{host_path}" if scheme else host_path
    if query:
        result += f"?{query}"
    return redactor.redact(result) if redactor else result


def endpoint_template(url: str) -> str:
    """Host + path only: the stable shape of an API endpoint for reports and docs."""
    scheme, _, rest = url.partition("://")
    host_path = rest.split("?", 1)[0].split("#", 1)[0]
    host_path = _UUID.sub("<id>", _LONG_HEX.sub("<id>", host_path))
    return f"{scheme}://{host_path}" if scheme else host_path


# --- HAR ------------------------------------------------------------------------------------------


def _sanitize_headers(headers: list[dict[str, Any]], redactor: Redactor) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for header in headers:
        name = str(header.get("name", ""))
        if name.lower() in SENSITIVE_HEADERS:
            kept.append({"name": name, "value": _REDACTED, "_botai": "stripped"})
        else:
            kept.append({"name": name, "value": redactor.redact(str(header.get("value", "")))})
    return kept


def _sanitize_named_values(
    params: list[dict[str, Any]], redactor: Redactor
) -> list[dict[str, Any]]:
    """Sanitize HAR `params`/`queryString` name-value pairs in place."""
    result: list[dict[str, Any]] = []
    for item in params:
        name = str(item.get("name", ""))
        lowered = name.lower()
        if lowered in PASSWORD_FIELDS | USERNAME_FIELDS | SENSITIVE_QUERY_KEYS:
            result.append({**item, "value": _REDACTED})
        else:
            result.append({**item, "value": redactor.redact(str(item.get("value", "")))})
    return result


def _sanitize_post_data(post: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    """Replace passwords/credentials in form bodies; keep only field names for auth posts."""
    mime = str(post.get("mimeType", ""))
    text = post.get("text")
    if not isinstance(text, str) or not text:
        return post
    sanitized = dict(post)
    lowered = text.lower()
    looks_like_credentials = any(name in lowered for name in PASSWORD_FIELDS | USERNAME_FIELDS)
    if "x-www-form-urlencoded" in mime:
        pairs: list[str] = []
        for chunk in text.split("&"):
            key, sep, value = chunk.partition("=")
            if key.lower() in PASSWORD_FIELDS or key.lower() in USERNAME_FIELDS:
                pairs.append(f"{key}{sep}{_REDACTED}")
            else:
                pairs.append(f"{key}{sep}{redactor.redact(value)}")
        sanitized["text"] = "&".join(pairs)
    elif looks_like_credentials or "json" in mime:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            sanitized["text"] = redactor.redact(text)
        else:
            sanitized["text"] = json.dumps(_sanitize_login_payload(payload), ensure_ascii=False)
    else:
        sanitized["text"] = redactor.redact(text)
    return sanitized


def _sanitize_login_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).lower() in PASSWORD_FIELDS | USERNAME_FIELDS:
                result[str(key)] = _REDACTED
            else:
                result[str(key)] = _sanitize_login_payload(value)
        return result
    if isinstance(payload, list):
        return [_sanitize_login_payload(item) for item in payload]
    return "<redacted>" if isinstance(payload, str) and len(payload) > 80 else payload


def sanitize_har(har_path: Path, redactor: Redactor) -> dict[str, Any]:
    """Rewrite one HAR file in place: no cookies, auth headers, passwords or response bodies.

    Returns a small summary (entry count, API endpoints) for the run report.
    """
    if not har_path.exists():
        return {"entries": 0, "sanitized": False}
    data = json.loads(har_path.read_text(encoding="utf-8"))
    log = data.setdefault("log", {})
    entries = log.get("entries", [])
    for entry in entries:
        request = entry.get("request", {})
        request["url"] = clean_url(str(request.get("url", "")), redactor=redactor)
        request["headers"] = _sanitize_headers(request.get("headers", []), redactor)
        request.pop("cookies", None)
        query = request.get("queryString")
        if isinstance(query, list):
            request["queryString"] = _sanitize_named_values(query, redactor)
        post = request.get("postData")
        if isinstance(post, dict):
            sanitized_post = _sanitize_post_data(post, redactor)
            params = sanitized_post.get("params")
            if isinstance(params, list):
                sanitized_post["params"] = _sanitize_named_values(params, redactor)
            request["postData"] = sanitized_post
        redirect_url = entry.get("redirectURL")
        if isinstance(redirect_url, str) and redirect_url:
            entry["redirectURL"] = clean_url(redirect_url, redactor=redactor)
        response = entry.get("response", {})
        response["headers"] = _sanitize_headers(response.get("headers", []), redactor)
        response.pop("cookies", None)
        content = response.get("content")
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str) and text:
                content["text"] = "<omitted: response bodies are not kept in HAR>"
            content.pop("_sha256", None)
        entry["_botai"] = {"sanitized": True}
    log["comment"] = (
        "Sanitized by bench/live: cookies, auth headers, password/username fields and response "
        "bodies were removed. Raw session material lives outside the repository."
    )
    # Last pass over the serialized document: exact registered values cannot survive anywhere.
    har_path.write_text(redactor.redact(json.dumps(data, ensure_ascii=False)), encoding="utf-8")
    return {"entries": len(entries), "sanitized": True}


def write_private_json(path: Path, payload: Any, redactor: Redactor | None = None) -> None:
    """Write JSON with mode 0600, redacting registered secrets when a redactor is given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if redactor is not None:
        payload = redactor.redact_payload(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)

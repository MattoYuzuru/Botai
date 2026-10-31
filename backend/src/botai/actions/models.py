"""What the agent may propose: one validated action request (HTTP or browser), nothing else.

A proposal is the unit the owner sees and the journal stores. It carries the exact request —
transport, method, URL, headers, body/cookies, browser steps — and can be hashed canonically:
the hash is the request's identity for derived idempotency keys and for the journal, while the
values themselves are masked before they are persisted (see `redact.py`).

Classification is intentionally conservative: the model may name a class, but never lower what
the request implies (`policy.infer_action_class`), so a `POST` cannot be presented as a read.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from botai.actions.redact import (
    SENSITIVE_KEY,
    body_hash,
    mask_secrets,
    mask_text,
    public_header_names,
)

Transport = Literal["http", "browser"]
ActionClass = Literal["read", "write_soft", "write_destructive", "money", "auth"]
ActionMode = Literal["auto", "ask", "never"]
ScopeKind = Literal["global", "site", "space", "source"]

# The write methods an action may use. Reads stay `http_request` / `browser_*`; a "write" action
# that is a GET has nothing to journal and nothing to protect.
WRITE_METHODS: tuple[str, ...] = ("POST", "PUT", "PATCH", "DELETE")
BODY_KINDS: tuple[str, ...] = ("json", "form", "raw")
BROWSER_ACTIONS: tuple[str, ...] = ("navigate", "click", "type", "press", "select")

JOURNAL_VALUE_MAX = 400


class BrowserStep(BaseModel):
    """One browser operation of a browser action (the run owns the real browser)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["navigate", "click", "type", "press", "select"]
    url: str | None = Field(default=None, max_length=4096)
    target: dict[str, Any] | None = None
    text: str | None = Field(default=None, max_length=4000)
    key: str | None = Field(default=None, max_length=40)
    value: str | None = Field(default=None, max_length=2000)
    label: str | None = Field(default=None, max_length=200)
    index: int | None = Field(default=None, ge=0, le=500)
    clear: bool = True

    @model_validator(mode="after")
    def _required_fields(self) -> BrowserStep:
        if self.action == "navigate" and not self.url:
            raise ValueError("navigate требует url")
        if self.action in ("click", "type", "select") and not self.target:
            raise ValueError(f"{self.action} требует target")
        if self.action == "type" and self.text is None:
            raise ValueError("type требует text")
        if self.action == "press" and not self.key:
            raise ValueError("press требует key")
        if (
            self.action == "select"
            and self.value is None
            and self.label is None
            and self.index is None
        ):
            raise ValueError("select требует value, label или index")
        return self

    def public(self) -> dict[str, Any]:
        """The step as the owner sees it: targets and URLs exact, typed secret values masked."""
        shown: dict[str, Any] = {"action": self.action}
        if self.url:
            shown["url"] = self.url
        if self.target:
            shown["target"] = _public_target(self.target)
        if self.key:
            shown["key"] = self.key
        if self.value is not None:
            shown["value"] = self.value[:JOURNAL_VALUE_MAX]
        if self.label is not None:
            shown["label"] = self.label
        if self.index is not None:
            shown["index"] = self.index
        if self.text is not None:
            shown["text"] = (
                "***"
                if _sensitive_target(self.target)
                else mask_text(self.text)[:JOURNAL_VALUE_MAX]
            )
        return shown


def _public_target(target: dict[str, Any]) -> dict[str, Any]:
    shown: dict[str, Any] = {}
    for key, value in target.items():
        if isinstance(value, str):
            shown[key] = mask_text(value)[:JOURNAL_VALUE_MAX]
        elif isinstance(value, int | float | bool):
            shown[key] = value
    return shown


def _sensitive_target(target: dict[str, Any] | None) -> bool:
    if not target:
        return False
    haystack = " ".join(str(value) for value in target.values() if isinstance(value, str))
    return bool(SENSITIVE_KEY.search(haystack))


class ActionRequest(BaseModel):
    """One concrete write action, validated. It is shown, decided and (maybe) executed as-is."""

    model_config = ConfigDict(extra="forbid")

    transport: Transport
    intent: str = Field(min_length=3, max_length=300)
    method: str | None = None
    url: str | None = Field(default=None, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    body_kind: Literal["json", "form", "raw"] = "json"
    steps: tuple[BrowserStep, ...] = ()
    action_class: ActionClass | None = None
    source_id: str | None = Field(default=None, max_length=26)
    space_id: str | None = Field(default=None, max_length=26)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=120)
    undo_hint: str | None = Field(default=None, max_length=500)
    follow_redirects: bool = True
    timeout_s: float = Field(default=30.0, ge=0.5, le=300)

    @model_validator(mode="after")
    def _transport_shape(self) -> ActionRequest:
        if self.transport == "http":
            if self.steps:
                raise ValueError("browser_steps допустимы только для transport=browser")
            if not self.url:
                raise ValueError("HTTP-действию нужен url")
            if not self.url.startswith(("http://", "https://")):
                raise ValueError("url должен быть полным HTTP(S)-адресом")
            method = (self.method or "").strip().upper()
            if method not in WRITE_METHODS:
                raise ValueError(f"method должен быть одним из {', '.join(WRITE_METHODS)}")
            self.method = method
            if self.body_kind == "form":
                if self.body is None:
                    self.body = {}
                if not isinstance(self.body, dict):
                    raise ValueError("body_kind=form требует объект в body")
            elif (
                self.body_kind == "raw" and self.body is not None and not isinstance(self.body, str)
            ):
                raise ValueError("body_kind=raw требует строку в body")
        else:
            if not self.steps:
                raise ValueError("браузерному действию нужен хотя бы один шаг")
            if self.method is not None:
                raise ValueError("method относится к transport=http")
            if self.body is not None or self.headers or self.cookies:
                raise ValueError("тело/заголовки/cookie относятся к transport=http")
        return self

    def canonical(self) -> dict[str, Any]:
        """The exact request as sorted data: the identity used for hashing (values included)."""
        payload: dict[str, Any] = {
            "transport": self.transport,
            "method": self.method,
            "url": self.url,
            "headers": sorted(self.headers.items()),
            "cookies": sorted(self.cookies.items()),
            "body": self.body,
            "body_kind": self.body_kind,
            "steps": [step.model_dump(mode="json") for step in self.steps],
        }
        return payload

    def canonical_hash(self) -> str:
        """sha256 of the canonical request: stable across key order, never contains the values."""
        encoded = json.dumps(
            self.canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    def body_sha256(self) -> str | None:
        """Hash of the exact body/steps: what the journal keeps instead of the values."""
        if self.transport == "browser":
            steps = json.dumps(
                [step.model_dump(mode="json") for step in self.steps],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return hashlib.sha256(steps.encode()).hexdigest()
        if self.body is None:
            return None
        return body_hash(self.body, self.body_kind)

    def public_preview(self) -> dict[str, Any]:
        """The redacted proposal the owner sees and the journal persists (never secret values)."""
        preview: dict[str, Any] = {
            "transport": self.transport,
            "intent": self.intent,
            "source_id": self.source_id,
            "space_id": self.space_id,
            "undo_hint": self.undo_hint,
        }
        if self.transport == "http":
            preview.update(
                {
                    "method": self.method,
                    "url": self.url,
                    "body_kind": self.body_kind,
                    "header_names": public_header_names(self.headers),
                    "cookie_names": sorted(self.cookies),
                }
            )
            if self.body_kind == "form" and isinstance(self.body, dict):
                preview["fields"] = [
                    {"name": name, "value": _shown_value(mask_secrets(value, name))}
                    for name, value in sorted(self.body.items())
                ]
            elif self.body is not None:
                preview["body"] = _shown_body(self.body, self.body_kind)
        else:
            preview["url"] = self.url
            preview["steps"] = [step.public() for step in self.steps]
        return preview

    def request_summary(self) -> dict[str, Any]:
        """The executed request without its values: method, URL, kind, byte count, body hash."""
        body = self.body if self.transport == "http" else self.steps
        if body is None:
            size = 0
        elif isinstance(body, str):
            size = len(body.encode())
        elif isinstance(body, bytes):
            size = len(body)
        else:
            size = len(json.dumps(body, ensure_ascii=False, default=str, sort_keys=True).encode())
        return {
            "transport": self.transport,
            "method": self.method,
            "url": self.url,
            "body_kind": self.body_kind if self.transport == "http" else "steps",
            "body_bytes": size,
            "body_sha256": self.body_sha256(),
            "step_count": len(self.steps),
            "follow_redirects": self.follow_redirects,
        }


def _shown_body(body: Any, body_kind: str) -> Any:
    if body_kind == "raw":
        return mask_text(str(body))[:JOURNAL_VALUE_MAX]
    masked = mask_secrets(body)
    encoded = json.dumps(masked, ensure_ascii=False, default=str)
    if len(encoded) <= JOURNAL_VALUE_MAX:
        return masked
    return {"preview": encoded[:JOURNAL_VALUE_MAX], "truncated": True}


def _shown_value(value: Any) -> str:
    if isinstance(value, str):
        return mask_text(value)[:JOURNAL_VALUE_MAX]
    return json.dumps(value, ensure_ascii=False, default=str)[:JOURNAL_VALUE_MAX]


__all__ = [
    "BODY_KINDS",
    "BROWSER_ACTIONS",
    "JOURNAL_VALUE_MAX",
    "WRITE_METHODS",
    "ActionClass",
    "ActionMode",
    "ActionRequest",
    "BrowserStep",
    "ScopeKind",
    "Transport",
]

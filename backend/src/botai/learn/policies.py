"""Canonical per-request auth when combining verified and already-shared recipe parts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from botai.core.errors import BotaiError, ErrorCode


def bind_parts(
    auth: Mapping[str, Any], endpoints: list[dict[str, Any]], bootstrap: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Mutate only copied request dictionaries; preserve each request's original authority.

    Promotion of a legacy policy is equivalent to that stored policy. It does not fetch excluded
    endpoints or infer a new credential source. New author candidates already have compiled bindings.
    """
    session = auth.get("type") == "session"
    if auth.get("type") not in {"none", "cookie", "bearer", "session"}:
        raise BotaiError(ErrorCode.RECIPE_REJECTED, "Неизвестный способ авторизации")
    if (
        not session
        and auth.get("type") == "bearer"
        and (auth.get("headerName") or "authorization").lower() != "authorization"
    ):
        raise BotaiError(
            ErrorCode.RECIPE_REJECTED,
            "Прежний способ авторизации нельзя объединить с новой привязкой",
        )
    requests = [step for step in bootstrap]
    requests.extend(
        endpoint["request"] for endpoint in endpoints if not endpoint["request"].get("render")
    )
    result: dict[str, dict[str, Any]] = {}
    names = {
        (False, False): "bNone",
        (True, False): "bCookies",
        (False, True): "bBearer",
        (True, True): "bCookiesBearer",
    }
    for request in requests:
        if session:
            binding = auth.get("bindings", {}).get(request.get("authBinding"))
            if not isinstance(binding, dict) or set(binding) != {"cookies", "bearer"}:
                raise BotaiError(
                    ErrorCode.RECIPE_REJECTED, "Не найдена проверенная привязка авторизации"
                )
        else:
            binding = {
                "cookies": auth.get("type") == "cookie",
                "bearer": "observedAuthorization" if auth.get("type") == "bearer" else "none",
            }
        if type(binding["cookies"]) is not bool or not isinstance(binding["bearer"], str) or binding["bearer"] not in {
            "none",
            "observedAuthorization",
        }:
            raise BotaiError(ErrorCode.RECIPE_REJECTED, "Некорректная привязка авторизации")
        name = names[binding["cookies"], binding["bearer"] == "observedAuthorization"]
        request["authBinding"] = name
        result[name] = dict(binding)
    return result

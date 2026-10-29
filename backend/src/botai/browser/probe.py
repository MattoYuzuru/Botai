"""Is a session still good, and which request proves it? Always through the EgressProxy.

* `check_session` asks the learned probe (or, failing that, the login-page redirect signs) whether
  a stored session still works. A network failure is raised, not read as "expired".
* `learn_probe` finds, among the requests the logged-in page itself made, a plain GET that answers
  with the session and refuses without it. That request becomes `success.probe` of the preset.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

from botai.browser.egress import Egress
from botai.browser.forms import has_credential_fields, looks_personal, safe_path
from botai.browser.session import httpx_cookies, request_headers
from botai.contracts.specs import LoginPreset
from botai.core.browser import ObservedRequest, PageSnapshot, SessionState, auth_origin
from botai.core.diagnostics import diagnostic_event, url_facts
from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import RetryPolicy, build_client, request_with_retries

POLICY = RetryPolicy(attempts=2, backoff_s=(0.4,), max_retry_after_s=5.0, jitter=0.1)
BUDGET_S = 20.0
MAX_CANDIDATES = 6
_REFUSALS = frozenset({301, 302, 303, 307, 308, 401, 403})
_AUTH_REQUIRED_TEXT = re.compile(
    r"вы не вошли|для продолжения[^.\n]{0,60}войдите|необходимо авторизоваться|"
    r"you are not (?:logged|signed) in|please (?:log|sign) in|authentication required",
    re.IGNORECASE,
)


def requires_authentication(snapshot: PageSnapshot) -> bool:
    """A generic restored-page refusal, independent of the successful HTTP probe."""
    if has_credential_fields(snapshot):
        return True
    try:
        if any(
            value in {"login_required", "interaction_required", "consent_required"}
            for value in parse_qs(urlsplit(snapshot.url).query).get("error", [])
        ):
            return True
    except ValueError:
        pass
    text = snapshot.text
    if snapshot.html:
        tree = LexborHTMLParser(snapshot.html)
        if not snapshot.forms and tree.css_first("input[type=password]") is not None:
            return True
        for node in tree.css("script,style,template,nav,header,aside,footer"):
            node.decompose()
        content = tree.css_first("main,[role=main]") or tree.body or tree.root
        if content is not None:
            # An explanatory paragraph in a long authenticated page is not an expired session.
            for heading in content.css("h1,h2,[role=heading]")[:2]:
                if _AUTH_REQUIRED_TEXT.search(heading.text()[:300]):
                    return True
            text = content.text(separator=" ", strip=True)
    return len(text) <= 1800 and bool(_AUTH_REQUIRED_TEXT.search(text[:1000]))


async def _status(
    egress: Egress,
    hosts: Sequence[str],
    url: str,
    *,
    state: SessionState | None,
    follow: bool = False,
) -> httpx.Response:
    creds = await egress.credentials(hosts, ttl_s=120)
    async with build_client(
        allow_hosts=hosts,
        proxy_url=creds.url,
        cookies=httpx_cookies(state) if state else None,
        headers=request_headers(state, url=url) if state else None,
    ) as client:
        audience = auth_origin(url)

        async def scope_credentials(request: httpx.Request) -> None:
            if auth_origin(str(request.url)) != audience:
                request.extensions["botai_probe_crossed"] = True
            if request.extensions.get("botai_probe_crossed"):
                request.headers.pop("cookie", None)
                request.headers.pop("authorization", None)

        client.event_hooks["request"].append(scope_credentials)
        return await request_with_retries(
            client, "GET", url, policy=POLICY, budget_s=BUDGET_S, follow_redirects=follow
        )


async def probe_ok(
    state: SessionState, probe: Mapping[str, Any], hosts: Sequence[str], *, egress: Egress
) -> bool:
    """Does the learned probe answer what it answered at login?"""
    response = await _status(egress, hosts, str(probe["url"]), state=state)
    passed = response.status_code == int(probe["expectStatus"])
    diagnostic_event(
        "auth.probe",
        status=response.status_code,
        expected_status=int(probe["expectStatus"]),
        passed=passed,
        bearer_present=state.bearer is not None,
        cookie_count=len(state.cookies),
        **url_facts(str(probe["url"])),
    )
    return passed


def _areas(preset: LoginPreset) -> list[str]:
    raw: Any = preset.expired.get("redirectPathPrefix") or []
    return [str(p) for p in raw]


async def check_session(
    state: SessionState,
    preset: LoginPreset | None,
    hosts: Sequence[str],
    *,
    now: datetime,
    egress: Egress,
) -> bool | None:
    """True: works. False: expired. None: nothing to check it with."""
    if state.expires_hint is not None and state.expires_hint <= now:
        diagnostic_event("auth.check_session", expired_hint=True, passed=False)
        return False
    if preset is None:
        diagnostic_event("auth.check_session", preset_present=False)
        return None
    probe: Any = preset.success.get("probe")
    try:
        if probe:
            return await probe_ok(state, probe, hosts, egress=egress)
        areas = _areas(preset)
        if not areas:
            diagnostic_event(
                "auth.check_session", probe_present=False, redirect_check_present=False
            )
            return None
        response = await _status(egress, hosts, preset.start["url"], state=state, follow=True)
    except BotaiError as exc:
        if exc.code == ErrorCode.EGRESS_BLOCKED:  # the site sent us to a host we never learned
            diagnostic_event("auth.check_session", egress_blocked=True, passed=False)
            return False
        raise
    if response.status_code in (401, 403):
        diagnostic_event("auth.check_session", status=response.status_code, passed=False)
        return False
    passed = not any(response.url.path.startswith(a) for a in areas)
    diagnostic_event(
        "auth.check_session",
        status=response.status_code,
        passed=passed,
        redirect_check_present=True,
        **url_facts(str(response.url)),
    )
    return passed


def candidates(
    log: Sequence[ObservedRequest],
    hosts: Sequence[str],
    final_url: str,
    avoid: Sequence[str] = (),
) -> list[str]:
    """GET URLs (path only) the page made after login, JSON first, the landing page last.
    Paths holding something the student typed (`avoid`) are never candidates, and the landing page
    only when it is a shallow, shared address (`/app`), not `/students/<someone>/dashboard`."""
    allowed = {h.lower() for h in hosts}
    json_first: list[str] = []
    other: list[str] = []
    for r in log:
        if r.method != "GET" or r.status is None or not 200 <= r.status < 300:
            continue
        if r.resource_type not in ("xhr", "fetch"):
            continue
        parts = urlsplit(r.url)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in allowed:
            continue
        url = f"https://{parts.netloc}{parts.path}"
        if (
            not parts.path
            or looks_personal(parts.path)
            or safe_path(parts.path, avoid) != parts.path
            or any(t in parts.path.lower() for t in avoid)
        ):
            continue
        bucket = json_first if "json" in (r.response_content_type or "") else other
        if url not in json_first and url not in other:
            bucket.append(url)
    final = urlsplit(final_url)
    landing = f"https://{final.netloc}{final.path}"
    if (
        final.scheme == "https"
        and (final.hostname or "").lower() in allowed
        and not looks_personal(final.path)
        and safe_path(final.path, avoid) == final.path
        and len([s for s in final.path.split("/") if s]) <= 1
        and landing not in json_first
        and landing not in other
    ):
        other.append(landing)
    return (json_first + other)[:MAX_CANDIDATES]


async def learn_probe(
    log: Sequence[ObservedRequest],
    state: SessionState,
    hosts: Sequence[str],
    final_url: str,
    *,
    egress: Egress,
    avoid: Sequence[str] = (),
) -> dict[str, Any] | None:
    """A request that answers 2xx with the session and is refused without it, or None."""
    for url in candidates(log, hosts, final_url, avoid):
        try:
            with_session = await _status(egress, hosts, url, state=state)
            if not 200 <= with_session.status_code < 300:
                continue
            anonymous = await _status(egress, hosts, url, state=None)
        except BotaiError as exc:
            if exc.code == ErrorCode.EGRESS_BLOCKED:
                continue
            raise
        if anonymous.status_code in _REFUSALS:
            diagnostic_event(
                "auth.probe_learned",
                status=with_session.status_code,
                anonymous_status=anonymous.status_code,
                **url_facts(url),
            )
            return {"method": "GET", "url": url, "expectStatus": with_session.status_code}
    return None

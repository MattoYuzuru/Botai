"""Live L3 scenarios of the BotAI v4 benchmark: login, session, API discovery, counts, actions.

Every scenario has the same shape: a list of measured steps, exact barriers, metrics and artifact
paths. The primary secrets are typed into real form fields from the secret broker and are never
recorded; the run knows only their registered redaction values.

Scenarios:

* ``hse-login``   -- real Chrome, empty profile, Keycloak login with the Keychain credentials from
  ``botai.live.hse.*``; then authenticated read of the schedule page, API discovery, counts,
  a safe GET and one safe GET-form (Moodle course search).
* ``hse-session`` -- session extraction: cookies of the owner's Chrome profile (copied, external)
  drive the same authenticated read without entering a password.
* ``cu-tasks``    -- Central University "Actual Student Tasks": Chrome session, API endpoints that
  load tasks, record counts, safe GET; cross-check with the ``central-university-lms`` reference
  harness on the same exported storage state.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import browser as browser_mod
from .browser import BrowserConfig, BrowserSession, cookie_summary
from .network import (
    NetworkRecorder,
    ScenarioEvidence,
    count_records,
    dom_counts,
    form_fields,
    payload_shape,
    save_screenshot,
    settle,
)
from .paths import RunDir, iso_now
from .redact import Redactor, write_private_json
from .secret_broker import get_secret

HSE_ENTRY = "https://lk.hse.ru/"
HSE_SCHEDULE = "https://lk.hse.ru/schedule?begin_date={date}"
HSE_SCHEDULE_API = "https://lk.hse.ru/api/get-myschedule?begin_date={date}"
HSE_MOODLE_COURSES = "https://edu.hse.ru/my/courses.php"
HSE_MOODLE_SEARCH = "https://edu.hse.ru/course/search.php"
HSE_KEYCLOAK = (
    "https://saml.hse.ru/realms/hse/protocol/openid-connect/auth"
    "?client_id=elk&redirect_uri=https%3A%2F%2Flk.hse.ru%2Fapi%2Fkeycloak-auth%2F"
    "&response_type=code&scope=openid"
)

CU_TASKS_PAGE = "https://my.centraluniversity.ru/learn/tasks/actual-student-tasks"
CU_TASKS_API = "https://my.centraluniversity.ru/api/micro-lms/tasks/student"
CU_API_PREFIX = "https://my.centraluniversity.ru/api/"
CU_SKILL_DIR = Path("~/.agents/skills/central-university-lms").expanduser()
CU_SKILL_PYTHON = Path(
    "~/.cache/yuzuru-codex-skills/central-university-lms/venv/bin/python"
).expanduser()
SEARCH_QUERY = "математика"  # public read-only query text, no personal data

LOGIN_TIMEOUT_MS = 90_000
_POLL_MS = 1_000


# --- result model --------------------------------------------------------------------------------


@dataclass(slots=True)
class ScenarioResult:
    """One measured scenario: steps, barriers, metrics, proof and artifact paths."""

    id: str
    title: str
    level: str = "L3"
    status: str = "failed"
    started_at: str = field(default_factory=iso_now)
    finished_at: str | None = None
    duration_sec: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    barriers: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    proof: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    _started_monotonic: float = field(default_factory=time.monotonic)

    def step(
        self, name: str, ok: bool, detail: str = "", *, duration_sec: float | None = None
    ) -> None:
        entry: dict[str, Any] = {"name": name, "ok": bool(ok), "detail": detail}
        if duration_sec is not None:
            entry["durationSec"] = round(duration_sec, 3)
        self.steps.append(entry)

    def barrier(self, code: str, detail: str, evidence: str = "") -> None:
        self.barriers.append({"code": code, "detail": detail, "evidence": evidence})

    def artifact(self, run: RunDir, kind: str, path: Path, note: str = "") -> None:
        self.artifacts.append({"kind": kind, "path": run.relative(path), "note": note})

    def finish(self, status: str | None = None) -> ScenarioResult:
        self.finished_at = iso_now()
        self.duration_sec = round(time.monotonic() - self._started_monotonic, 3)
        if status is not None:
            self.status = status
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "level": self.level,
            "status": self.status,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "durationSec": self.duration_sec,
            "steps": self.steps,
            "barriers": self.barriers,
            "metrics": self.metrics,
            "proof": self.proof,
            "artifacts": self.artifacts,
            "error": self.error,
        }


# --- small helpers -------------------------------------------------------------------------------


def week_monday(today: date | None = None) -> str:
    moment = today or date.today()
    return (moment - timedelta(days=moment.weekday())).isoformat()


def _local_storage_keys(page: Any) -> list[str]:
    try:
        return sorted(str(key) for key in page.evaluate("() => Object.keys(localStorage)"))
    except Exception:
        return []


def _endpoint_statuses(network: NetworkRecorder, marker: str) -> list[int]:
    return [entry.status for entry in network.entries if marker in entry.url]


def _is_authenticated_api(network: NetworkRecorder, marker: str) -> bool:
    statuses = _endpoint_statuses(network, marker)
    return bool(statuses) and any(200 <= status < 300 for status in statuses)


def _visible_alert_text(page: Any) -> str:
    for selector in (
        ".kc-feedback-text",
        "#input-error",
        ".pf-c-alert__title",
        ".alert-error",
        "[role=alert]",
        ".error-message",
    ):
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                continue
            text = (locator.first.inner_text(timeout=1500) or "").strip()
            if text:
                return text[:400]
        except Exception:
            continue
    return ""


def _has_otp(page: Any) -> bool:
    for selector in (
        "input[name=otp]",
        "#kc-otp-login-form",
        "input[autocomplete='one-time-code']",
        "#kc-totp-login-form",
    ):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def _has_captcha(page: Any) -> bool:
    for selector in (
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        ".g-recaptcha",
        ".h-captcha",
        "#kc-captcha",
        "input[name='captcha']",
    ):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def _classify_login_error(text: str) -> str:
    lowered = text.casefold()
    if "invalid" in lowered or "неверн" in lowered or "incorrect" in lowered:
        return "invalid_credentials"
    if "отключ" in lowered or "disabled" in lowered or "заблокир" in lowered:
        return "account_locked"
    if "слишком много" in lowered or "too many" in lowered or "rate" in lowered:
        return "rate_limited"
    return "login_rejected"


def _find_login_form(page: Any, timeout_ms: int = 30_000) -> bool:
    try:
        page.wait_for_selector(
            "input[name=username], #username, input[name=email], input[type=email]",
            timeout=timeout_ms,
            state="visible",
        )
        return True
    except Exception:
        return False


def _click_login_button(page: Any) -> bool:
    for selector in (
        "button:has-text('Войти')",
        "a:has-text('Войти')",
        "input[type=submit][value*='Войти']",
        "text=Войти",
    ):
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                continue
            locator.first.click(timeout=5_000)
            return True
        except Exception:
            continue
    return False


def _fill_credentials(page: Any, login: str, password: str) -> bool:
    """Type the broker values into the real form. Values never leave this function."""
    filled = 0
    for selector, value in (
        ("input[name=username], #username, input[name=email], input[type=email]", login),
        ("input[name=password], #password, input[type=password]", password),
    ):
        try:
            locator = page.locator(selector).first
            locator.fill(value, timeout=10_000)
            filled += 1
        except Exception:
            continue
    return filled == 2


def _submit_login(page: Any) -> bool:
    for selector in (
        "input[name=login]",
        "button[type=submit]",
        "input[type=submit]",
        "#kc-login",
    ):
        try:
            locator = page.locator(selector).first
            locator.click(timeout=5_000)
            return True
        except Exception:
            continue
    return False


def _wait_login_outcome(page: Any, network: NetworkRecorder, timeout_s: float) -> dict[str, Any]:
    """Poll the page after the submit; classify success, MFA, captcha or an explicit rejection."""
    deadline = time.monotonic() + timeout_s
    last_url = ""
    while time.monotonic() < deadline:
        try:
            if page.is_closed():
                return {"state": "crashed", "detail": "page closed while waiting for login"}
            url = page.url
            last_url = url
            alert = _visible_alert_text(page)
            if alert:
                return {"state": _classify_login_error(alert), "detail": alert, "url": url}
            if _has_otp(page):
                return {
                    "state": "mfa_required",
                    "detail": "the IdP asked for a one-time code after the password step",
                    "url": url,
                }
            if _has_captcha(page):
                return {
                    "state": "captcha_required",
                    "detail": "the IdP showed a captcha after the password step",
                    "url": url,
                }
            if "saml.hse.ru" not in url and "lk.hse.ru" in url and "error=" not in url:
                if _is_authenticated_api(network, "/api/"):
                    return {"state": "authenticated", "detail": "returned to lk.hse.ru", "url": url}
                # The SPA needs a moment to exchange the code and mount.
                try:
                    page.wait_for_timeout(2_500)
                except Exception:
                    pass
                if "saml.hse.ru" not in page.url and "lk.hse.ru" in page.url:
                    return {"state": "returned", "detail": "returned to lk.hse.ru", "url": page.url}
        except Exception:
            pass
        try:
            page.wait_for_timeout(_POLL_MS)
        except Exception:
            break
    return {
        "state": "timeout",
        "detail": f"login did not finish in {timeout_s:.0f}s",
        "url": last_url,
    }


# --- HSE read pipeline (shared by login and session scenarios) ------------------------------------


def _read_hse_schedule(
    page: Any,
    context: Any,
    network: NetworkRecorder,
    result: ScenarioResult,
    run: RunDir,
) -> dict[str, Any]:
    """Dynamic page + API discovery + entity counts + one safe GET."""
    monday = week_monday()
    metrics: dict[str, Any] = {}
    started = time.monotonic()
    url = HSE_SCHEDULE.format(date=monday)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
        settle(page, 5_000)
        result.step(
            "hse.schedule.open", True, f"opened {url}", duration_sec=time.monotonic() - started
        )
    except Exception as exc:
        result.step("hse.schedule.open", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        result.barrier("schedule_page_failed", str(exc)[:200])
        return metrics

    dom = dom_counts(page)
    metrics["dom"] = dom
    metrics["pageTitle"] = _safe_title(page)
    body_text = _page_text(page)
    metrics["bodyTextLength"] = len(body_text)
    metrics["timePairsInDom"] = dom.get("timeLike", 0)

    payloads = network.json_payloads(url_contains=("/api/get-myschedule",))
    records = 0
    shapes: list[dict[str, Any]] = []
    for item in payloads:
        counted = count_records(item["payload"])
        records = max(records, int(counted["listRecords"]))
        shapes.append(
            {
                "endpoint": item["endpoint"],
                "shape": payload_shape(item["payload"]),
                "counts": counted,
            }
        )
    metrics["scheduleRecords"] = records
    metrics["apiPayloads"] = shapes
    result.step(
        "hse.schedule.parse",
        records > 0,
        f"{records} schedule records from {len(payloads)} JSON responses; "
        f"{dom.get('timeLike', 0)} time-like strings in DOM",
    )

    endpoints = network.api_endpoints()
    metrics["apiEndpoints"] = endpoints
    metrics["apiEndpointCount"] = len(endpoints)
    result.step(
        "hse.api.discover",
        len(endpoints) > 0,
        f"{len(endpoints)} distinct XHR/API endpoints from page traffic",
    )

    # One safe GET: read-only schedule API through the authenticated browser context.
    get_url = HSE_SCHEDULE_API.format(date=monday)
    safe_get = _safe_get(context, page, get_url, result, "hse.api.safe_get")
    metrics["safeGet"] = safe_get
    api_counts = safe_get.get("counts") or {}
    api_records = int(api_counts.get("listRecords") or 0)
    api_days = int(api_counts.get("days") or 0)
    api_lessons = int(api_counts.get("lessons") or 0)
    metrics["scheduleApiRecords"] = api_records
    metrics["scheduleApiDays"] = api_days
    metrics["scheduleApiPairs"] = api_lessons
    metrics["scheduleDeadlineLikeRecords"] = int(api_counts.get("deadlineLikeRecords") or 0)
    metrics["scheduleRecords"] = max(int(metrics.get("scheduleRecords") or 0), api_records)
    result.step(
        "hse.schedule.api",
        api_records > 0,
        f"authenticated GET {get_url} -> {safe_get.get('status')}: "
        f"{api_days} days, {api_lessons} lessons, {api_records} JSON records",
    )
    return metrics


def _safe_get(
    context: Any,
    page: Any,
    url: str,
    result: ScenarioResult,
    step_name: str,
) -> dict[str, Any]:
    """Run one read-only GET; only status and structural counts leave this function."""
    started = time.monotonic()
    record: dict[str, Any] = {"url": url, "method": "GET"}
    try:
        response = context.request.get(url, timeout=30_000)
        record["status"] = response.status
        if response.ok:
            try:
                payload = response.json()
                record["shape"] = payload_shape(payload)
                record["counts"] = count_records(payload)
            except Exception:
                record["shape"] = {"type": "non-json"}
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    # The SPA calls the API with a bearer token from localStorage; read it inside the page,
    # where the token is available, and bring back counts only.
    in_page = _in_page_get(page, url)
    record["inPage"] = in_page
    if in_page.get("status") and 200 <= in_page["status"] < 300:
        record["status"] = in_page["status"]
        if in_page.get("json"):
            record["counts"] = {
                "listRecords": in_page.get("listRecords"),
                "recordLikeRecords": in_page.get("recordLike"),
                "deadlineLikeRecords": in_page.get("deadlineLike"),
                "days": in_page.get("days"),
                "lessons": in_page.get("lessons"),
            }
    ok = bool(record.get("status") and 200 <= record["status"] < 300)
    result.step(
        step_name,
        ok,
        f"GET {url} -> {record.get('status', 'error')}"
        + (
            f", records={record.get('counts', {}).get('listRecords')}"
            if record.get("counts")
            else ""
        ),
        duration_sec=time.monotonic() - started,
    )
    if not ok:
        result.barrier("safe_get_failed", f"GET {url} -> {record.get('status')}")
    return record


_IN_PAGE_GET_JS = """async (u) => {
    const tokenKeys = ['access_token', 'accessToken', 'token', 'id_token'];
    let token = null;
    for (const key of tokenKeys) {
        const raw = localStorage.getItem(key);
        if (!raw) continue;
        try {
            const parsed = JSON.parse(raw);
            token = (parsed && parsed.access_token) || (typeof parsed === 'string' ? parsed : token);
        } catch (e) { token = raw; }
        if (token) break;
    }
    const headers = token ? {Authorization: 'Bearer ' + token} : {};
    try {
        const r = await fetch(u, {headers, credentials: 'include'});
        const text = await r.text();
        let j = null;
        try { j = JSON.parse(text); } catch (e) { return {status: r.status, json: false}; }
        let listRecords = 0;
        let recordLike = 0;
        let deadlineLike = 0;
        const recordKey = /(id|name|title|date|start|end|time|deadline|due|state|status|subject|discipline|auditor|room|task|event|lesson|course|exercise|score|mark)/i;
        const deadlineKey = /(deadline|due)/i;
        const walk = (x, depth) => {
            if (depth > 8 || listRecords >= 5000) return;
            if (Array.isArray(x)) {
                for (const item of x) {
                    if (item && typeof item === 'object' && !Array.isArray(item)) {
                        listRecords += 1;
                        const keys = Object.keys(item);
                        if (keys.some(k => recordKey.test(k))) recordLike += 1;
                        if (keys.some(k => deadlineKey.test(k))) deadlineLike += 1;
                    }
                    walk(item, depth + 1);
                }
            } else if (x && typeof x === 'object') {
                for (const value of Object.values(x)) walk(value, depth + 1);
            }
        };
        walk(j, 0);
        let days = 0;
        let lessons = 0;
        const walkLessons = (x, depth) => {
            if (depth > 8 || lessons >= 5000) return;
            if (Array.isArray(x)) {
                for (const item of x) walkLessons(item, depth + 1);
            } else if (x && typeof x === 'object') {
                if (Object.prototype.hasOwnProperty.call(x, 'date')) days += 1;
                for (const [k, v] of Object.entries(x)) {
                    if (Array.isArray(v)) {
                        if (/^lessons?$|^classes$|^events$|^pairs$/i.test(k)) lessons += v.length;
                        walkLessons(v, depth + 1);
                    } else {
                        walkLessons(v, depth + 1);
                    }
                }
            }
        };
        walkLessons(j, 0);
        const top = (j && typeof j === 'object' && !Array.isArray(j)) ? j : {};
        return {
            status: r.status,
            json: true,
            listRecords: listRecords,
            recordLike: recordLike,
            deadlineLike: deadlineLike,
            days: days,
            lessons: lessons,
            keys: Object.keys(top).slice(0, 20),
            listLengths: Object.entries(top).filter((e) => Array.isArray(e[1])).slice(0, 20).map((e) => [e[0], e[1].length]),
        };
    } catch (e) { return {status: 0, error: String(e).slice(0, 100)}; }
}"""


def _in_page_get(page: Any, url: str) -> dict[str, Any]:
    """Authenticated in-page read that returns only structural counts (never values)."""
    try:
        return dict(page.evaluate(_IN_PAGE_GET_JS, url))
    except Exception as exc:
        return {"status": 0, "error": f"{type(exc).__name__}: {str(exc)[:100]}"}


def _read_hse_moodle(
    context: Any,
    network: NetworkRecorder,
    result: ScenarioResult,
    run: RunDir,
    output_dir: Path,
    *,
    allow_form_action: bool,
) -> dict[str, Any]:
    """Cross-service SSO check + one safe GET form (Moodle course search, no writes)."""
    metrics: dict[str, Any] = {}
    page = context.new_page()
    network.attach(page)
    try:
        page.goto(HSE_MOODLE_COURSES, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
        settle(page, 3_000)
        if "login" in page.url:
            clicked = _click_login_button(page)
            result.step("hse.moodle.sso_click", clicked, f"entry page {page.url[:120]}")
            page.wait_for_timeout(6_000)
        authenticated = "login" not in page.url and "hse.ru" in page.url
        courses = _count_moodle_courses(page)
        if authenticated and courses == 0:
            settle(page, 4_000)
            courses = _count_moodle_courses(page)
        metrics["moodleAuthenticated"] = authenticated
        metrics["moodleCourses"] = courses
        metrics["moodleLinks"] = _count_links(page, ("/course/",))
        metrics["moodleBodyTextLength"] = len(_page_text(page))
        metrics["moodleUrl"] = page.url[:160]
        result.step(
            "hse.moodle.read",
            authenticated,
            f"authenticated={authenticated}, course links={courses}, url={page.url[:120]}",
        )
        if not authenticated:
            result.barrier("sso_reuse_failed", "Moodle did not accept the IdP session")
        if allow_form_action and authenticated:
            form_metrics = _moodle_search_action(page, network, result, output_dir)
            metrics.update(form_metrics)
        save_screenshot(page, output_dir / "screenshots" / "moodle-courses.png", full_page=False)
    except Exception as exc:
        result.step("hse.moodle.read", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        result.barrier("moodle_failed", str(exc)[:200])
    finally:
        try:
            page.close()
        except Exception:
            pass
    return metrics


def _count_moodle_courses(page: Any) -> int:
    try:
        return int(
            page.evaluate(
                "() => new Set(Array.from(document.querySelectorAll('a[href*=\"/course/view.php\"]'))"
                ".map(a => a.getAttribute('href'))).size"
            )
        )
    except Exception:
        return 0


def _moodle_search_action(
    page: Any,
    network: NetworkRecorder,
    result: ScenarioResult,
    output_dir: Path,
) -> dict[str, Any]:
    """One safe GET form: the course search submits a plain GET query, no state changes."""
    metrics: dict[str, Any] = {"formAction": {"page": "edu.hse.ru/course/search.php"}}
    started = time.monotonic()
    try:
        page.goto(HSE_MOODLE_SEARCH, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
        settle(page, 2_000)
        fields = form_fields(page)
        metrics["formAction"]["fields"] = fields
        filled = False
        for selector in (
            "input[name=search]",
            "#search",
            "input[type=search]",
            "input[name=q]",
            "input[name=query]",
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                locator.fill(SEARCH_QUERY, timeout=5_000)
                filled = True
                break
            except Exception:
                continue
        metrics["formAction"]["filled"] = filled
        results_before = _count_moodle_courses(page)
        if filled:
            try:
                page.keyboard.press("Enter")
            except Exception:
                page.locator("input[type=submit]").first.click(timeout=5_000)
            settle(page, 3_000)
        results_after = _count_moodle_courses(page)
        metrics["formAction"]["courseLinksAfterSearch"] = results_after
        metrics["formAction"]["courseLinksBeforeSearch"] = results_before
        metrics["formAction"]["submittedGet"] = filled and page.url != HSE_MOODLE_SEARCH
        result.step(
            "hse.moodle.form_action",
            filled,
            f"typed a public search query; GET results={results_after} (no write request was made)",
            duration_sec=time.monotonic() - started,
        )
        save_screenshot(page, output_dir / "screenshots" / "moodle-search.png")
    except Exception as exc:
        metrics["formAction"]["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        result.step("hse.moodle.form_action", False, str(exc)[:200])
    return metrics


def _safe_title(page: Any) -> str:
    try:
        return str(page.title())[:160]
    except Exception:
        return ""


def _page_text(page: Any) -> str:
    try:
        return str(page.evaluate("() => document.body ? document.body.innerText : ''"))
    except Exception:
        return ""


# --- scenario: HSE login with Keychain credentials ------------------------------------------------


def scenario_hse_login(run: RunDir, redactor: Redactor, config: BrowserConfig) -> ScenarioResult:
    """Real Chrome, empty profile, credentials from the macOS Keychain, typed by this code only."""
    result = ScenarioResult(id="hse-login", title="ВШЭ: живой вход по кредам из Keychain")
    evidence_dir = run.scenario_dir("hse-login")
    evidence = ScenarioEvidence("hse-login", evidence_dir, redactor)
    session: BrowserSession | None = None
    context: Any = None
    try:
        result.metrics["browserMode"] = "chrome-fresh"
        session = browser_mod.start_browser(replace(config, mode="chrome-fresh"), evidence_dir)
        result.metrics["browserVersion"] = session.chrome_version
        har_path = evidence_dir / "network.har"
        context = session.new_context(har_path=har_path)
        page = context.new_page()
        evidence.network.attach(page)
        evidence.console.attach(page)

        # Fetch the primary secrets only now, register them for redaction and type them in.
        login = get_secret("hse_mail")
        password = get_secret("hse_password")
        redactor.register(login)
        redactor.register(password)
        result.metrics["credentials"] = {
            "source": "macOS Keychain",
            "services": ["botai.live.hse.mail", "botai.live.hse.password"],
            "visibleToModelOrLogs": False,
            "fieldsFilled": 0,
        }
        result.proof["loginUrl"] = HSE_ENTRY

        started = time.monotonic()
        page.goto(HSE_ENTRY, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
        settle(page, 2_000)
        result.step("hse.entry", True, f"opened {HSE_ENTRY}, title={_safe_title(page)}")

        clicked = _click_login_button(page)
        page.wait_for_timeout(3_000)
        if not _find_login_form(page, timeout_ms=12_000):
            if clicked:
                result.step("hse.login.button", True, "clicked the entry-page login button")
            page.goto(HSE_KEYCLOAK, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
            settle(page, 1_500)
        if not _find_login_form(page, timeout_ms=25_000):
            result.step("hse.login.form", False, f"no login form at {page.url[:160]}")
            result.barrier(
                "login_form_not_found", "the IdP login form did not appear", _safe_title(page)
            )
            save_screenshot(page, evidence_dir / "screenshots" / "login-form-missing.png")
            return result.finish("blocked")
        result.step("hse.login.form", True, f"IdP login form at {page.url[:160]}")
        result.metrics["loginFormFields"] = form_fields(page)
        save_screenshot(page, evidence_dir / "screenshots" / "login-form.png")

        if not _fill_credentials(page, login, password):
            result.step("hse.login.fill", False, "could not fill both credential fields")
            result.barrier(
                "login_fields_not_fillable", "username/password fields were not fillable"
            )
            return result.finish("failed")
        result.step(
            "hse.login.fill", True, "typed login and password from the broker (values not logged)"
        )
        result.metrics["credentials"]["fieldsFilled"] = 2

        if not _submit_login(page):
            result.step("hse.login.submit", False, "submit control not found")
            result.barrier("login_submit_not_found", "no submit control on the IdP form")
            return result.finish("failed")
        result.step("hse.login.submit", True, "submitted the IdP login form")

        outcome = _wait_login_outcome(page, evidence.network, LOGIN_TIMEOUT_MS / 1000)
        login_duration = round(time.monotonic() - started, 3)
        result.metrics["loginDurationSec"] = login_duration
        state = str(outcome.get("state"))
        result.proof["loginOutcome"] = {
            "state": state,
            "detail": redactor.redact(str(outcome.get("detail", "")))[:300],
        }

        if state not in {"authenticated", "returned"}:
            label = {
                "mfa_required": "IdP требует MFA/одноразовый код",
                "captcha_required": "IdP показывает капчу",
                "invalid_credentials": "IdP отклонил креды",
                "account_locked": "аккаунт заблокирован IdP",
                "rate_limited": "IdP ограничил частоту попыток",
                "timeout": "логин не завершился за отведённое время",
            }.get(state, "логин отклонён")
            result.step("hse.login.outcome", False, f"{state}: {label}")
            result.barrier(
                state,
                redactor.redact(str(outcome.get("detail", "")))[:300],
                str(outcome.get("url", ""))[:160],
            )
            save_screenshot(page, evidence_dir / "screenshots" / "login-blocked.png")
            return result.finish("blocked")

        # Confirm the authenticated state with a real authenticated read and record the session.
        confirm_started = time.monotonic()
        metrics = _read_hse_schedule(page, context, evidence.network, result, run)
        result.metrics.update(metrics)
        authenticated = bool(evidence.network.api_endpoints()) and (
            _is_authenticated_api(evidence.network, "/api/get-user")
            or _is_authenticated_api(evidence.network, "/api/get-myschedule")
            or _is_authenticated_api(evidence.network, "/api/")
        )
        result.proof["authenticated"] = authenticated
        result.metrics["session"] = _session_snapshot(context, page, ("hse.ru",))
        result.metrics["session"]["localStorageKeys"] = _local_storage_keys(page)
        write_private_json(
            evidence_dir / "storage-state.json",
            context.storage_state(),
            redactor,
        )
        result.artifact(
            run,
            "storage_state",
            evidence_dir / "storage-state.json",
            "Playwright storage state of the live HSE session (0600, outside repo)",
        )
        save_screenshot(page, evidence_dir / "screenshots" / "hse-schedule.png")
        moodle = _read_hse_moodle(
            context, evidence.network, result, run, evidence_dir, allow_form_action=True
        )
        result.metrics.update({"moodle": moodle})

        evidence.network.save(evidence_dir / "network.json")
        evidence.console.save(evidence_dir / "console.json")
        result.artifact(run, "har", har_path, "sanitized HAR of the login and read flow")
        result.artifact(
            run,
            "network",
            evidence_dir / "network.json",
            "API endpoint discovery from observed traffic",
        )
        result.artifact(run, "console", evidence_dir / "console.json", "browser console, redacted")
        result.artifact(
            run, "screenshots", evidence_dir / "screenshots", "login and schedule frames"
        )
        result.step(
            "hse.login.confirm",
            authenticated and bool(result.proof.get("loginOutcome")),
            f"authenticated={authenticated}; "
            f"schedule records={result.metrics.get('scheduleRecords', 0)}; "
            f"login took {login_duration}s",
            duration_sec=time.monotonic() - confirm_started,
        )
        status = (
            "success"
            if authenticated and result.metrics.get("scheduleRecords", 0) > 0
            else "partial"
        )
        if status == "partial":
            result.barrier(
                "read_incomplete", "login succeeded but the dynamic read found no records"
            )
        return result.finish(status)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        result.barrier("scenario_error", result.error)
        return result.finish("failed")
    finally:
        _close_quietly(context, run, evidence_dir, result, evidence)
        if session is not None:
            session.close()


# --- scenario: HSE session extracted from the owner's Chrome -------------------------------------


def scenario_hse_session(run: RunDir, redactor: Redactor, config: BrowserConfig) -> ScenarioResult:
    """Session extraction: profile cookies drive an authenticated read without a password."""
    result = ScenarioResult(id="hse-session", title="ВШЭ: чтение на сессии Chrome (без пароля)")
    evidence_dir = run.scenario_dir("hse-session")
    evidence = ScenarioEvidence("hse-session", evidence_dir, redactor)
    session: BrowserSession | None = None
    context: Any = None
    try:
        session = browser_mod.start_browser(replace(config, mode="chrome-copy"), evidence_dir)
        result.metrics["browserMode"] = "chrome-copy"
        result.metrics["browserVersion"] = session.chrome_version
        all_cookies = session.profile_cookies()
        hse_cookies = [c for c in all_cookies if str(c.get("domain", "")).endswith("hse.ru")]
        summary = cookie_summary(all_cookies, ("hse.ru",))
        result.metrics["cookieSummary"] = summary
        result.step(
            "hse.session.extract",
            bool(hse_cookies),
            f"{len(hse_cookies)} HSE cookies in the copied Chrome profile "
            f"({len(summary.get('domains', {}))} domains)",
        )
        if not hse_cookies:
            result.barrier("hse_session_missing", "no HSE cookies in the Chrome profile")
            return result.finish("blocked")

        har_path = evidence_dir / "network.har"
        context = session.new_context(har_path=har_path, cookies=hse_cookies)
        page = context.new_page()
        evidence.network.attach(page)
        evidence.console.attach(page)
        page.goto(HSE_ENTRY, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
        settle(page, 2_500)
        result.proof["entryTitle"] = _safe_title(page)
        metrics = _read_hse_schedule(page, context, evidence.network, result, run)
        result.metrics.update(metrics)
        authenticated = _is_authenticated_api(evidence.network, "/api/get-myschedule") or (
            _is_authenticated_api(evidence.network, "/api/")
        )
        result.proof["authenticated"] = authenticated
        result.metrics["session"] = _session_snapshot(context, page, ("hse.ru",))
        result.metrics["session"]["localStorageKeys"] = _local_storage_keys(page)
        write_private_json(evidence_dir / "storage-state.json", context.storage_state(), redactor)
        result.artifact(
            run,
            "storage_state",
            evidence_dir / "storage-state.json",
            "extracted HSE session (0600, outside repo)",
        )
        save_screenshot(page, evidence_dir / "screenshots" / "hse-schedule.png")
        moodle = _read_hse_moodle(
            context, evidence.network, result, run, evidence_dir, allow_form_action=True
        )
        result.metrics["moodle"] = moodle
        evidence.network.save(evidence_dir / "network.json")
        evidence.console.save(evidence_dir / "console.json")
        result.artifact(run, "har", har_path, "sanitized HAR of the session read")
        result.artifact(run, "network", evidence_dir / "network.json", "API endpoint discovery")
        result.artifact(run, "console", evidence_dir / "console.json", "browser console, redacted")
        result.step(
            "hse.session.confirm",
            authenticated,
            f"authenticated={authenticated}; schedule records="
            f"{result.metrics.get('scheduleRecords', 0)}",
        )
        if not authenticated:
            result.barrier("hse_session_stale", "HSE API rejected the extracted session")
            return result.finish("blocked")
        status = "success" if result.metrics.get("scheduleRecords", 0) > 0 else "partial"
        return result.finish(status)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        result.barrier("scenario_error", result.error)
        return result.finish("failed")
    finally:
        _close_quietly(context, run, evidence_dir, result, evidence)
        if session is not None:
            session.close()


# --- scenario: Central University actual student tasks --------------------------------------------


def scenario_cu_tasks(run: RunDir, redactor: Redactor, config: BrowserConfig) -> ScenarioResult:
    """ЦУ «Actual Student Tasks»: live session from Chrome, endpoints, counts, reference harness."""
    result = ScenarioResult(id="cu-tasks", title="ЦУ: Actual Student Tasks на живой сессии")
    evidence_dir = run.scenario_dir("cu-tasks")
    evidence = ScenarioEvidence("cu-tasks", evidence_dir, redactor)
    session: BrowserSession | None = None
    context: Any = None
    try:
        session = browser_mod.start_browser(replace(config, mode="chrome-copy"), evidence_dir)
        result.metrics["browserMode"] = "chrome-copy"
        result.metrics["browserVersion"] = session.chrome_version
        # Source 1: the session saved by the CU skill (may be stale). Source 2: Chrome cookies.
        skill_state_status = _reference_harness_session_check(None, result)
        result.metrics["referenceHarnessSavedSession"] = skill_state_status
        all_cookies = session.profile_cookies()
        cu_cookies = [
            c
            for c in all_cookies
            if str(c.get("domain", "")).endswith(("centraluniversity.ru", "cu.ru"))
        ]
        summary = cookie_summary(all_cookies, ("centraluniversity.ru", "cu.ru"))
        result.metrics["cookieSummary"] = summary
        result.step(
            "cu.session.extract",
            bool(cu_cookies),
            f"{len(cu_cookies)} CU cookies in the copied Chrome profile",
        )
        if not cu_cookies:
            result.barrier("cu_session_missing", "no CU cookies in the Chrome profile")
            return result.finish("blocked")

        har_path = evidence_dir / "network.har"
        context = session.new_context(har_path=har_path, cookies=cu_cookies)
        page = context.new_page()
        evidence.network.attach(page)
        evidence.console.attach(page)
        result.proof["tasksUrl"] = CU_TASKS_PAGE
        started = time.monotonic()
        page.goto(CU_TASKS_PAGE, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
        settle(page, 3_000)
        render_started = time.monotonic()
        rendered = False
        for _ in range(20):
            length = len(_page_text(page))
            links = _count_links(page, ("/learn/tasks/", "/learn/courses/", "longread"))
            if length > 2_000 or links > 5:
                rendered = True
                break
            page.wait_for_timeout(500)
        result.metrics["renderWaitMs"] = round((time.monotonic() - render_started) * 1000)
        result.step(
            "cu.tasks.open",
            True,
            f"opened {CU_TASKS_PAGE}; dynamic render={'yes' if rendered else 'timeout'} "
            f"after {result.metrics['renderWaitMs']}ms",
            duration_sec=time.monotonic() - started,
        )
        result.proof["finalUrl"] = page.url[:160]
        result.proof["pageTitle"] = _safe_title(page)
        dom = dom_counts(page)
        result.metrics["dom"] = dom
        body_text = _page_text(page)
        result.metrics["bodyTextLength"] = len(body_text)
        task_links = _count_links(page, ("/learn/tasks/", "/learn/courses/", "longread"))
        result.metrics["taskLinksInDom"] = task_links
        save_screenshot(page, evidence_dir / "screenshots" / "actual-student-tasks.png")

        endpoints = evidence.network.api_endpoints()
        result.metrics["apiEndpoints"] = endpoints
        result.metrics["apiEndpointCount"] = len(endpoints)
        result.step(
            "cu.api.discover",
            len(endpoints) > 0,
            f"{len(endpoints)} distinct CU API/XHR endpoints while loading the page",
        )

        payloads = evidence.network.json_payloads(url_contains=("/api/micro-lms/tasks",))
        records = 0
        payload_shapes: list[dict[str, Any]] = []
        for item in payloads:
            counted = count_records(item["payload"])
            records = max(records, int(counted["listRecords"]))
            payload_shapes.append(
                {
                    "endpoint": item["endpoint"],
                    "shape": payload_shape(item["payload"]),
                    "counts": counted,
                }
            )
        result.metrics["taskRecords"] = records
        result.metrics["taskPayloads"] = payload_shapes
        authenticated = _is_authenticated_api(evidence.network, "/api/micro-lms/tasks/student") or (
            _is_authenticated_api(evidence.network, "/api/account/me")
        )
        result.proof["authenticated"] = authenticated
        result.step(
            "cu.tasks.parse",
            records > 0,
            f"{records} task records from {len(payloads)} JSON responses; "
            f"{task_links} task-like links in DOM",
        )

        safe_get = _safe_get(context, page, CU_TASKS_API, result, "cu.api.safe_get")
        result.metrics["safeGet"] = safe_get
        api_counts = safe_get.get("counts") or {}
        result.metrics["tasksApiRecords"] = int(api_counts.get("listRecords") or 0)
        result.metrics["tasksApiDeadlineLikeRecords"] = int(
            api_counts.get("deadlineLikeRecords") or 0
        )
        result.metrics["taskRecords"] = max(
            int(result.metrics.get("taskRecords") or 0), int(api_counts.get("listRecords") or 0)
        )
        form_probe = _cu_form_probe(page, result)
        result.metrics["formProbe"] = form_probe
        result.metrics["session"] = _session_snapshot(
            context, page, ("centraluniversity.ru", "cu.ru")
        )
        result.metrics["session"]["localStorageKeys"] = _local_storage_keys(page)
        write_private_json(evidence_dir / "storage-state.json", context.storage_state(), redactor)
        result.artifact(
            run,
            "storage_state",
            evidence_dir / "storage-state.json",
            "extracted CU session (0600, outside repo)",
        )

        # Cross-check the same exported session with the external reference harness (CU skill).
        reference = _run_reference_harness(
            evidence_dir / "storage-state.json", evidence_dir, run, result
        )
        result.metrics["referenceHarness"] = reference
        result.step(
            "cu.reference_harness",
            bool(reference.get("sessionCheck", {}).get("ready")),
            f"external harness session-check={reference.get('sessionCheck', {}).get('status')}, "
            f"active tasks={reference.get('deadlinesCount')}",
        )

        evidence.network.save(evidence_dir / "network.json")
        evidence.console.save(evidence_dir / "console.json")
        result.artifact(run, "har", har_path, "sanitized HAR of the CU tasks page")
        result.artifact(
            run,
            "network",
            evidence_dir / "network.json",
            "CU API endpoint discovery (task-loading endpoints)",
        )
        result.artifact(run, "console", evidence_dir / "console.json", "browser console, redacted")
        result.artifact(
            run, "screenshots", evidence_dir / "screenshots", "Actual Student Tasks frame"
        )
        if not authenticated:
            result.barrier("cu_session_stale", "the CU API rejected the extracted session")
            return result.finish("blocked")
        status = "success" if records > 0 else "partial"
        if status == "partial":
            result.barrier(
                "cu_tasks_empty", "session works but the tasks payload has no record-like objects"
            )
        return result.finish(status)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        result.barrier("scenario_error", result.error)
        return result.finish("failed")
    finally:
        _close_quietly(context, run, evidence_dir, result, evidence)
        if session is not None:
            session.close()


def _count_links(page: Any, markers: tuple[str, ...]) -> int:
    try:
        return int(
            page.evaluate(
                """(markers) => new Set(Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.getAttribute('href') || '')
                    .filter(href => markers.some(marker => href.includes(marker)))).size""",
                list(markers),
            )
        )
    except Exception:
        return 0


def _cu_form_probe(page: Any, result: ScenarioResult) -> dict[str, Any]:
    """Fill one search field without submitting: a form interaction, no write request."""
    probe: dict[str, Any] = {"submitted": False}
    try:
        fields = form_fields(page)
        probe["fields"] = fields
        for selector in (
            "input[type=search]",
            "input[name=search]",
            "input[placeholder*='оиск']",
            "input[placeholder*='earch']",
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible():
                    continue
                locator.fill(SEARCH_QUERY, timeout=5_000)
                probe["typedIntoSearch"] = True
                break
            except Exception:
                continue
        result.step(
            "cu.form.probe",
            "typedIntoSearch" in probe,
            "typed a public query into a search field; nothing was submitted",
        )
    except Exception as exc:
        probe["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        result.step("cu.form.probe", False, str(exc)[:160])
    return probe


# --- reference harness (external CU skill) --------------------------------------------------------


def _run_json_command(
    command: list[str], timeout: int = 120, cwd: Path | None = None
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
        )
        if proc.returncode != 0:
            return {
                "status": "error",
                "returncode": proc.returncode,
                "detail": (proc.stderr or proc.stdout or "")[:200],
            }
        output = json.loads(proc.stdout)
        return {"status": "ok", "payload": output}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": f"no answer in {timeout}s"}
    except (json.JSONDecodeError, ValueError) as exc:
        return {"status": "unparsable", "detail": str(exc)[:200]}
    except Exception as exc:
        return {"status": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _reference_harness_session_check(
    storage_state: Path | None, result: ScenarioResult
) -> dict[str, Any]:
    """Check the skill's own saved session (no exported state needed)."""
    if not CU_SKILL_PYTHON.exists() or not (CU_SKILL_DIR / "scripts" / "lms.py").exists():
        return {"status": "unavailable", "detail": "skill runtime is not installed"}
    command = [str(CU_SKILL_PYTHON), str(CU_SKILL_DIR / "scripts" / "lms.py"), "session-check"]
    if storage_state is not None:
        command.extend(["--storage-state", str(storage_state)])
    answer = _run_json_command(command, cwd=CU_SKILL_DIR)
    if answer.get("status") != "ok":
        return {"status": answer.get("status"), "detail": answer.get("detail", "")[:200]}
    payload = answer.get("payload") or {}
    return {
        "status": payload.get("status"),
        "publishedCourseCount": payload.get("publishedCourseCount"),
        "ready": payload.get("status") == "ready",
    }


def _count_json_items(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("items", "courses", "deadlines", "data", "results", "tasks"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        for value in payload.values():
            if isinstance(value, list):
                return len(value)
    return 0


def _run_reference_harness(
    state_path: Path, evidence_dir: Path, run: RunDir, result: ScenarioResult
) -> dict[str, Any]:
    """Run the external CU skill against the same exported session and compare observability."""
    reference: dict[str, Any] = {
        "harness": "central-university-lms skill (Playwright, headless)",
        "skillDir": "~/.agents/skills/central-university-lms",
    }
    if not CU_SKILL_PYTHON.exists():
        reference["status"] = "unavailable"
        reference["detail"] = "skill runtime venv not found"
        result.step("cu.reference_harness", False, "skill runtime is not installed")
        return reference
    if not state_path.exists():
        reference["status"] = "no_state"
        result.step("cu.reference_harness", False, "no exported storage state to compare")
        return reference

    lms_script = str(CU_SKILL_DIR / "scripts" / "lms.py")
    check = _run_json_command(
        [str(CU_SKILL_PYTHON), lms_script, "session-check", "--storage-state", str(state_path)],
        cwd=CU_SKILL_DIR,
    )
    session_check: dict[str, Any] = {"status": check.get("status")}
    if check.get("status") == "ok":
        payload = check.get("payload") or {}
        session_check.update(
            {
                "ready": payload.get("status") == "ready",
                "publishedCourseCount": payload.get("publishedCourseCount"),
            }
        )
    else:
        session_check["detail"] = check.get("detail", "")[:200]
    reference["sessionCheck"] = session_check

    courses = _run_json_command(
        [
            str(CU_SKILL_PYTHON),
            lms_script,
            "list-courses",
            "--storage-state",
            str(state_path),
            "--limit",
            "50",
        ],
        cwd=CU_SKILL_DIR,
    )
    if courses.get("status") == "ok":
        payload = courses.get("payload")
        reference["coursesCount"] = _count_json_items(payload)
        write_private_json(evidence_dir / "reference-harness-courses.json", payload)
    else:
        reference["coursesError"] = courses.get("detail", "")[:200]

    deadlines = _run_json_command(
        [
            str(CU_SKILL_PYTHON),
            lms_script,
            "deadlines",
            "--storage-state",
            str(state_path),
            "--limit",
            "20",
        ],
        cwd=CU_SKILL_DIR,
    )
    if deadlines.get("status") == "ok":
        payload = deadlines.get("payload")
        reference["deadlinesCount"] = _count_json_items(payload)
        write_private_json(evidence_dir / "reference-harness-deadlines.json", payload)
    else:
        reference["deadlinesError"] = deadlines.get("detail", "")[:200]
    reference["status"] = "ok" if session_check.get("ready") else "failed"
    artifact = evidence_dir / "reference-harness-deadlines.json"
    if artifact.exists():
        result.artifact(
            run,
            "reference_harness",
            artifact,
            "reference harness output on the same exported session (raw, private)",
        )
    return reference


# --- session snapshot -----------------------------------------------------------------------------


def _session_snapshot(context: Any, page: Any, suffixes: tuple[str, ...]) -> dict[str, Any]:
    """Cookie/localStorage summary of a live session; values are never included."""
    try:
        cookies = list(context.cookies())
    except Exception:
        cookies = []
    summary = cookie_summary(cookies, suffixes)
    return {
        "matchedCookies": summary.get("matches", 0),
        "domains": {
            domain: {"count": bucket["count"], "names": bucket["names"][:12]}
            for domain, bucket in summary.get("domains", {}).items()
        },
        "totalCookiesInContext": len(cookies),
        "storageStateExported": True,
    }


def _close_quietly(
    context: Any,
    run: RunDir,
    evidence_dir: Path,
    result: ScenarioResult,
    evidence: ScenarioEvidence,
) -> None:
    """Close the context (this flushes the HAR) and sanitize the file."""
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass
    har = evidence_dir / "network.har"
    if har.exists():
        from .redact import sanitize_har

        info = sanitize_har(har, evidence.redactor)
        har.chmod(0o600)
        result.metrics["har"] = info
        existing = {artifact["path"] for artifact in result.artifacts}
        relative = run.relative(har)
        if relative not in existing:
            result.artifact(
                run, "har", har, "sanitized HAR (cookies, auth headers, bodies removed)"
            )
    try:
        evidence.network.save(evidence_dir / "network.json")
        evidence.console.save(evidence_dir / "console.json")
    except Exception:
        pass

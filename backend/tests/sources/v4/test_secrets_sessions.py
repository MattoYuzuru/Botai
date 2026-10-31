"""Real sessions of a v4 source: Vault handles, storage-state cookies, bearer tokens, calendars.

No secrets are passed as constructor arguments here: the source's `auth_ref` is a Vault handle
(`<kind>:<scope>`) and the runner resolves it through the same reader the production wiring uses.
A missing session is a loud configuration error, not an empty header that would read as "no data".
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from botai.agent.v4.factory import VaultSecretReader
from botai.core.errors import BotaiError, ErrorCode
from botai.core.vault import InMemoryVault, SecretKind
from botai.db.models import ScheduleEvent
from botai.sources.v4 import playbooks, service
from botai.sources.v4.runner import SourceRunner
from botai.sources.v4.schemas import SourcePatch
from botai.sources.v4.secrets import VaultSessionReader, parse_auth_ref
from tests.auth.support import Sm
from tests.fakeuni import ical
from tests.fakeuni.login import COOKIE, SPA_JWT
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db

BASE = "https://fakeuni.test"


async def make_source(
    sm: Sm,
    account: Account,
    *,
    url: str | None = f"{BASE}/app",
    link: str | None = None,
    auth_ref: str | None = None,
    title: str = "Кабинет",
) -> Any:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title=title,
            entry_url=url if link is None else None,
            link=link,
            kinds=["site", "schedule"],
            auth_ref=auth_ref,
        )
        await session.commit()
        return source


async def attach_playbook(
    sm: Sm,
    account: Account,
    source: Any,
    *,
    spec: dict[str, Any],
    code: str | None = None,
    language: str = "python",
) -> None:
    async with sm() as session:
        playbook = await playbooks.create_playbook(
            session,
            user_id=account.user_id,
            title="Календарь",
            university_id=account.university_id,
            language=language,
        )
        version = await playbooks.add_playbook_version(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            spec=spec,
            code=code,
        )
        await playbooks.record_canary(
            session,
            user_id=account.user_id,
            playbook_id=playbook.id,
            version=version.version,
            canary={"ok": True},
        )
        await playbooks.activate_playbook_version(
            session, user_id=account.user_id, playbook_id=playbook.id, version=version.version
        )
        await service.update_source(
            session,
            user_id=account.user_id,
            source_id=source.id,
            patch=SourcePatch(playbook_id=playbook.id),
        )
        await session.commit()


def test_auth_ref_parsing_is_the_shared_kind_scope_convention() -> None:
    assert parse_auth_ref("token:calendar-1") == ("token", "calendar-1")
    assert parse_auth_ref(" session : scope ") == ("session", "scope")
    with pytest.raises(BotaiError) as exc:
        parse_auth_ref("no-kind-here")
    assert exc.value.code is ErrorCode.VALIDATION


async def test_vault_bearer_token_authenticates_an_http_read(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any
) -> None:
    vault = InMemoryVault()
    await vault.put(account.user_id, "spa-token", SecretKind.TOKEN, SPA_JWT)
    source = await make_source(
        tx_sessionmaker, account, url=f"{BASE}/spa", auth_ref="token:spa-token"
    )
    async with tx_sessionmaker() as session:
        result = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            secret_reader=VaultSecretReader(vault, user_id=account.user_id),
            env="test",
        ).read_live(source.id, url=f"{BASE}/spa/api/ping")
        await session.commit()
    assert result.ok, result.error
    assert "u-1001" in result.text
    assert SPA_JWT not in json.dumps(result.model_dump(mode="json"))


async def test_vault_storage_state_feeds_the_http_cookie_jar(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any, fakeuni_cookies: dict[str, str]
) -> None:
    vault = InMemoryVault()
    state = {
        "cookies": [
            {
                "name": COOKIE,
                "value": fakeuni_cookies[COOKIE],
                "domain": "fakeuni.test",
                "path": "/",
            },
        ],
        "origins": [],
    }
    await vault.put(account.user_id, "fakeuni-session", SecretKind.SESSION, json.dumps(state))
    source = await make_source(
        tx_sessionmaker, account, url=f"{BASE}/app", auth_ref="session:fakeuni-session"
    )
    async with tx_sessionmaker() as session:
        result = await SourceRunner(
            session,
            user_id=account.user_id,
            transport=fakeuni_transport,
            session_reader=VaultSessionReader(vault),
            env="test",
        ).read_live(source.id, url=f"{BASE}/api/v2/me")
        await session.commit()
    assert result.ok, result.error
    assert "Иванова" in result.text


class _Source:
    """The two attributes the session reader reads (a whole row is not needed)."""

    auth_ref = "token:scope"
    user_id = "user"


async def test_a_token_handle_is_not_a_session_and_is_left_to_the_bearer_reader() -> None:
    vault = InMemoryVault()
    await vault.put("user", "scope", SecretKind.TOKEN, "bearer-value")
    assert await VaultSessionReader(vault)(_Source()) is None  # type: ignore[arg-type]


async def test_a_missing_session_is_a_loud_configuration_error(
    tx_sessionmaker: Sm, account: Account, fakeuni_transport: Any
) -> None:
    vault = InMemoryVault()
    source = await make_source(
        tx_sessionmaker, account, url=f"{BASE}/app", auth_ref="session:absent"
    )
    async with tx_sessionmaker() as session:
        with pytest.raises(BotaiError) as exc:
            await SourceRunner(
                session,
                user_id=account.user_id,
                transport=fakeuni_transport,
                session_reader=VaultSessionReader(vault),
                env="test",
            ).read_live(source.id, url=f"{BASE}/api/v2/me")
    assert exc.value.code is ErrorCode.VALIDATION


async def test_browser_spec_without_a_browser_is_an_honest_run_error(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account)
    spec = {
        "version": 1,
        "format": "browser",
        "browser": {"steps": [{"action": "goto", "target": f"{BASE}/app"}]},
    }
    async with tx_sessionmaker() as session:
        report = await SourceRunner(session, user_id=account.user_id, env="test").probe(
            source.id, spec=spec
        )
    assert not report.ok
    assert report.errors[0].code == "browser_unavailable"


async def test_browser_format_without_a_plan_is_rejected_structurally(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        report = await SourceRunner(session, user_id=account.user_id, env="test").probe(
            source.id, spec={"version": 1, "format": "browser"}
        )
    assert not report.ok
    assert report.errors[0].code == "recipe_rejected"


FEED_CODE = """
import json
import re
from datetime import datetime, timedelta, timezone

raw = open("reads/feed.txt", encoding="utf-8").read()
records = []
for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
    uid = re.search(r"^UID:(.+)$", block, re.M)
    start = re.search(r"^DTSTART:(\\d{8}T\\d{6})Z$", block, re.M)
    summary = re.search(r"^SUMMARY:(.+)$", block, re.M)
    if not uid or not start or not summary:
        continue
    starts = datetime.strptime(start.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    records.append({
        "kind": "schedule",
        "external_id": uid.group(1).strip() + "@" + start.group(1),
        "fields": {
            "title": summary.group(1).strip(),
            "startsAt": starts.isoformat(),
            "endsAt": (starts + timedelta(minutes=80)).isoformat(),
        },
    })
print(json.dumps({"records": records}))
"""


async def test_two_calendars_of_one_vuz_coexist_and_write_their_own_rows(
    tx_sessionmaker: Sm,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
) -> None:
    first = await make_source(tx_sessionmaker, account, link=ical.URL, title="Календарь ЛМС")
    second = await make_source(
        tx_sessionmaker, account, link=ical.SERVICE_URL, title="Календарь сервиса"
    )
    assert first.identity_key != second.identity_key
    for source, feed_url in ((first, ical.URL), (second, ical.SERVICE_URL)):
        await attach_playbook(
            tx_sessionmaker,
            account,
            source,
            spec={
                "version": 1,
                "reads": [{"name": "feed", "url": feed_url, "format": "text"}],
                "timeout_s": 10,
            },
            code=FEED_CODE,
        )
    one = SourceRunner(
        tx_sessionmaker(),
        user_id=account.user_id,
        transport=fakeuni_transport,
        cookies=fakeuni_cookies,
        env="test",
    )
    reports = [await one.sync(first.id), await one.sync(second.id)]
    assert all(report.ok for report in reports), [report.errors for report in reports]
    assert reports[0].counts == reports[1].counts
    assert reports[0].counts["schedule"] >= 2
    async with tx_sessionmaker() as session:
        rows = (
            await session.scalars(
                select(ScheduleEvent).where(ScheduleEvent.source_id.in_([first.id, second.id]))
            )
        ).all()
        sources = await service.list_sources(session, user_id=account.user_id)
    assert {row.source_id for row in rows} == {first.id, second.id}
    per_source = {
        source_id: {row.external_id for row in rows if row.source_id == source_id}
        for source_id in (first.id, second.id)
    }
    assert per_source[first.id] == per_source[second.id]  # the same feed, two sources
    assert len(per_source[first.id]) >= 2
    assert {first.id, second.id} <= {source.id for source in sources}

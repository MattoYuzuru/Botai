"""A calendar by its export link, over the real stack: the protected field of a task, the Vault, and
the calendar fetched through the EgressProxy process with a grant for the host of the link alone.
Only the university (the calendar service of fakeuni) and the model are synthetic."""

from __future__ import annotations

import pytest

from botai.agent.tools_sources import connect_calendar
from botai.core.llm import ScriptedLlm
from botai.core.vault import SecretKind
from botai.worker import handler_table
from tests.e2e.support import Stack, WireTap, dump_database, leaks
from tests.fakeuni import ical as fakeuni
from tests.jobs.test_calendar_link import calendar_of, hand_over, history, said, student

pytestmark = [pytest.mark.db, pytest.mark.browser]
HOST = "calendar.fakeuni.test"


async def test_the_calendar_is_read_through_the_egress_proxy_with_a_grant_for_its_host(
    stack: Stack, wiretap: WireTap
) -> None:
    env = stack.env
    fakeuni.reset()
    env.services.llm = ScriptedLlm()
    env.start(env.worker(handler_table()))  # the handlers of production, no test transport
    who = await student(env)
    told = await connect_calendar(who.chat(env), {"university_query": "Фейкуни"})
    assert told.startswith("Результат: запущено")

    seen: set[str] = set()
    task = await hand_over(who, fakeuni.SERVICE_WEBCAL, seen)
    done = await env.wait_for(task, {"done"})
    assert done.result == {"status": "ok", "events": 7, "deadlines": 2}
    conn = await calendar_of(env, who.user_id)
    assert conn.status == "active"
    refresh = (await who.client.post(f"/api/v1/connections/{conn.id}/sync")).json()["jobId"]
    assert (await env.wait_for(refresh, {"done"})).result["status"] == "ok"  # type: ignore[index]

    # the proxy let through the host of the link and nothing else: one grant, one host
    tunnels = [line.split("egress host=")[1].split() for line in stack.proxy_log()]
    assert {fields[0] for fields in tunnels if "decision=allow" in fields} == {HOST}
    assert sum("decision=allow" in fields for fields in tunnels) == 2  # the check and the refresh
    assert {fields[2] for fields in tunnels if fields[1] == "decision=deny"} <= {
        "reason=auth_failed"
    }

    # the link: in the Vault, and nowhere else
    stored = await env.services.vault.get(who.user_id, conn.id, SecretKind.ICAL_URL)
    assert stored is not None
    assert stored.get_secret_value() == fakeuni.SERVICE_URL
    everything = "\n".join(
        [await dump_database(env.sm), wiretap.text, said(await history(who)), *stack.proxy.lines]
    )
    assert leaks(everything, {"key": fakeuni.SERVICE_TOKEN, "name": "private_token"}) == []

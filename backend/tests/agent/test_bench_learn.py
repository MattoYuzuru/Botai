"""B1-B3 of the benchmark on a scripted model: the wiring, the judging, the fuse (L1, no money).

The product code runs for real - Explorer, repair job, lint, canary, sync, Postgres - over the
synthetic fakeuni; only the model is scripted (`ScriptedLlm`), as in W3's story test.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from botai.agent import bench, bench_learn
from botai.agent.bench import Cell, Fuse, Launch, Ledger, RecordingLlm, RunRecord, summarize
from botai.agent.bench_learn import LEARNING_SCENARIOS, load_plans
from botai.agent.llm import load_profiles, parse_profile, resolve
from botai.core.llm import LlmProvider, LlmRequest, ScriptedLlm
from tests.agent.conftest import make_settings
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import data, pages
from tests.learn import support as ls
from tests.sources import recipes

CELL = Cell("flash", "high")


def launch(tmp_path: Path, **kw: Any) -> Launch:
    defaults: dict[str, Any] = {
        "settings": make_settings(llm_provider="mock"),
        "fuse": Fuse(Ledger(tmp_path / "ledger.json"), run_cap_usd=3.0),
        "replay": None,
        "record": None,
        "repeats": 1,
        "concurrency": 3,
        "only": set(),
    }
    return Launch(**{**defaults, **kw})


def use_model(monkeypatch: pytest.MonkeyPatch, provider: LlmProvider) -> None:
    monkeypatch.setattr(bench, "provider_for", lambda _l, _c: provider)


def moved_timetable() -> dict[str, Any]:
    return recipes.timetable_endpoint(path="/api/v3/schedule")


async def run(
    scenario: str, tmp_path: Path, **kw: Any
) -> tuple[list[RunRecord], str | None, Launch]:
    ln = launch(tmp_path, **kw)
    records, stop = await LEARNING_SCENARIOS[scenario].run(ln, [CELL])
    return records, stop, ln


# --- the reference ------------------------------------------------------------------------------


def test_the_reference_rows_are_the_data_of_fakeuni() -> None:
    """If W3 changes the synthetic university, the benchmark must say so, not drift."""
    plans = load_plans()
    ref = plans["B1"].reference
    events = [
        (e["subject"]["name"], datetime.fromisoformat(e["slot"]["begin"])) for e in data.timetable()
    ]
    assert [(r.title, r.at) for r in ref["scheduleEvents"]] == events
    deadlines = [
        (d["name"], datetime.fromtimestamp(int(str(d["due"])), tz=UTC)) for d in data.deadlines()
    ]
    assert [(r.title, r.at) for r in ref["deadlines"]] == deadlines
    assert [r.title for r in plans["B2"].reference["news"]] == [t for _, t, _, _ in pages.NEWS]
    assert (len(ref["scheduleEvents"]), len(ref["deadlines"])) == (4, 2)  # benchmark.md B1


def test_the_plans_say_what_each_scenario_is() -> None:
    plans = load_plans()
    assert {k: (p.job, p.kinds, p.max_questions) for k, p in plans.items()} == {
        "B1": ("connect", ("scheduleEvents", "deadlines"), 1),
        "B2": ("connect", ("news",), 1),
        "B3": ("repair", ("scheduleEvents", "deadlines"), None),
    }
    assert sorted(bench.scenarios()) == ["B1", "B2", "B3", "B4"]


# --- the cell's profile drives learn and repair ---------------------------------------------------


@pytest.mark.parametrize("purpose", ["learn", "repair", "agent"])
def test_the_cells_profile_drives_the_purpose(
    purpose: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    class Spy:
        def __init__(self, settings: Any, *, gate: Any) -> None:
            seen.append(settings)

    monkeypatch.setattr(bench, "DeepSeekProvider", Spy)
    bench.provider_for(launch(Path("/nonexistent")), Cell("pro", "max"))
    profiles = load_profiles(seen[0])
    wanted = parse_profile("deepseek-v4-pro:enabled:max")
    assert profiles[purpose] == wanted
    req = LlmRequest(messages=[], purpose=purpose)  # type: ignore[arg-type]
    resolved = resolve(req, seen[0], profiles)
    assert (resolved.model, resolved.thinking, resolved.effort) == (
        "deepseek-v4-pro",
        "enabled",
        "max",
    )
    assert profiles["extract"] == parse_profile(make_settings().llm_profile_extract)


# --- B1, B2, B3 on a perfect model --------------------------------------------------------------


async def test_b1_learns_the_json_university(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ls.EvidenceLlm(queue=[ls.timetable_answer(), ls.deadlines_answer()])
    use_model(monkeypatch, llm)
    records, stop, ln = await run("B1", tmp_path)

    assert stop is None
    (r,) = records
    assert (r.cell, r.case, r.kind, r.repeat) == ("flash/high", "B1", "connect", 0)
    assert r.success, r.reasons
    assert r.reasons == []
    assert r.questions == 1  # the confirmation of the summary, nothing else
    assert r.steps == 2
    assert (r.tokens_miss, r.tokens_hit, r.tokens_out) == (2_400, 0, 600)
    assert r.cost_usd > 0
    assert ln.fuse.spent_usd == pytest.approx(r.cost_usd)
    assert "scheduleEvents=4" in r.answer
    assert "deadlines=2" in r.answer
    assert "(1, 'active')" in r.answer
    assert [q.purpose for q in llm.requests] == ["learn", "learn"]
    assert fakeuni_api.STATE.timetable_version == "v2"


async def test_b2_learns_the_html_university(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ls.EvidenceLlm(queue=[ls.answer([recipes.news_endpoint()])])
    use_model(monkeypatch, llm)
    records, stop, _ = await run("B2", tmp_path)

    assert stop is None
    (r,) = records
    assert r.success, r.reasons
    assert (r.case, r.questions, r.steps) == ("B2", 1, 1)
    assert "news=2" in r.answer
    assert [q.purpose for q in llm.requests] == ["learn"]
    assert "news-item" in llm.requests[0].messages[1].content  # the page was shown to the model


async def test_b3_repairs_the_moved_endpoint_and_measures_only_the_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ls.EvidenceLlm(queue=[ls.answer([moved_timetable()])])
    use_model(monkeypatch, llm)
    records, stop, _ = await run("B3", tmp_path)

    assert stop is None
    (r,) = records
    assert r.success, r.reasons
    assert (r.case, r.kind, r.steps, r.questions) == ("B3", "repair", 1, 0)
    assert [q.purpose for q in llm.requests] == ["repair"]  # the setup syncs never call the model
    assert "status=repaired" in r.answer
    assert "(1, 'deprecated'), (2, 'active')" in r.answer
    assert r.tokens_miss == 1_200
    assert fakeuni_api.STATE.timetable_version == "v2"  # fakeuni is put back for the next run


# --- a model that does it wrong -----------------------------------------------------------------


async def test_wrong_fields_are_caught_by_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four rows are not enough: they must be the university's rows."""
    wrong = recipes.timetable_endpoint()
    wrong["response"]["fields"]["title"] = {"path": "/place/room", "required": True}
    use_model(monkeypatch, ls.EvidenceLlm(queue=[ls.answer([wrong]), ls.deadlines_answer()]))
    (r,), _, _ = await run("B1", tmp_path)

    assert not r.success
    assert r.error is None
    (reason,) = r.reasons
    assert reason.startswith("расписание: записей 4 вместо 4")
    assert "нет: «Математический анализ»" in reason
    assert "не те: «А-101»" in reason


async def test_a_second_question_fails_b1_even_with_the_right_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sources of deadlines that disagree on identity: the agent asks which one is the
    student's. The data end up right, but the student was bothered."""
    other = recipes.deadlines_endpoint()
    other["response"]["fields"]["externalId"] = {"path": "/name"}  # other ids, same rows
    both = ls.answer([recipes.deadlines_endpoint(), other])
    use_model(monkeypatch, ls.EvidenceLlm(queue=[ls.timetable_answer(), both]))
    (r,), _, _ = await run("B1", tmp_path)

    assert r.questions == 2
    assert not r.success
    assert r.reasons == ["студента спросили 2 раз, ожидалось не больше 1"]
    assert "deadlines=2" in r.answer


async def test_a_job_that_waits_for_an_answer_nobody_gives_is_a_failure_not_a_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model finds the timetable only; the Explorer offers to send a link; the scripted
    student has none."""
    use_model(monkeypatch, ls.EvidenceLlm(queue=[ls.timetable_answer(), ls.nothing()]))
    (r,), stop, _ = await run("B1", tmp_path)

    assert stop is None
    assert not r.success
    assert r.questions == 2  # "give a link?" and the link itself
    assert r.error is not None
    assert "ждало ответа студента" in r.reasons[0]
    assert r.steps == 2


async def test_b3_fails_when_the_repair_finds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, ls.EvidenceLlm(queue=[ls.nothing()] * 3))
    (r,), _, _ = await run("B3", tmp_path)

    assert not r.success
    assert r.reasons[0].startswith("задание закончилось со статусом «unresolved»")
    assert any("версии описания" in x for x in r.reasons)
    assert fakeuni_api.STATE.timetable_version == "v2"


async def test_a_scenario_that_cannot_be_prepared_stops_the_launch_before_any_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the model's fault, so no failed run is booked against it: the launch refuses."""
    llm = ScriptedLlm()
    use_model(monkeypatch, llm)
    monkeypatch.setattr(bench_learn.Kit, "move_timetable", lambda _self: None)  # nothing breaks
    ln = launch(tmp_path, repeats=2)
    with pytest.raises(bench_learn.SetupFailed, match="поломка не замечена"):
        await LEARNING_SCENARIOS["B3"].run(ln, [CELL])
    assert llm.requests == []
    assert ln.fuse.spent_usd == 0


# --- many runs ----------------------------------------------------------------------------------


async def test_every_run_has_its_own_database_so_none_reuses_anothers_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The learned recipe of an origin is shared between students: in one database the second run
    would find it and skip the model. Recorded answers are replayed by request, so any number of
    runs can share them."""
    fixtures = tmp_path / "fixtures"
    seed_llm = ls.EvidenceLlm(queue=[ls.timetable_answer(), ls.deadlines_answer()])
    use_model(monkeypatch, RecordingLlm(seed_llm, fixtures / "flash-high"))
    first, _, _ = await run("B1", tmp_path / "a")
    assert all(r.success for r in first)

    monkeypatch.undo()
    records, stop, ln = await run("B1", tmp_path / "b", replay=fixtures, repeats=3)
    assert stop is None
    assert [r.repeat for r in records] == [0, 1, 2]
    assert [r.success for r in records] == [True] * 3, [r.reasons for r in records]
    assert [r.steps for r in records] == [2, 2, 2]  # nobody skipped the learning
    assert ln.fuse.spent_usd == pytest.approx(sum(r.cost_usd for r in records))


async def test_a_blown_fuse_stops_the_launch_and_keeps_the_finished_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = tmp_path / "fixtures"
    use_model(
        monkeypatch,
        RecordingLlm(
            ls.EvidenceLlm(queue=[ls.timetable_answer(), ls.deadlines_answer()]),
            fixtures / "flash-high",
        ),
    )
    (ok,), _, _ = await run("B1", tmp_path / "a")
    monkeypatch.undo()

    ln = launch(tmp_path / "b", replay=fixtures, repeats=3, concurrency=1)
    ln.fuse.run_cap_usd = ok.cost_usd * 1.5  # one run and a half
    records, stop = await LEARNING_SCENARIOS["B1"].run(ln, [CELL])
    assert stop is not None
    assert "launch cap" in stop
    assert len(records) == 1
    assert records[0].success


# --- the machinery around -----------------------------------------------------------------------


async def test_without_the_test_helpers_the_scenarios_say_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> Any:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(bench_learn, "import_module", missing)
    for scenario in ("B1", "B2", "B3"):
        with pytest.raises(bench.ScenarioUnavailable, match=r"backend/tests"):
            await LEARNING_SCENARIOS[scenario].run(launch(tmp_path), [CELL])


async def test_command_line_replay_writes_a_learning_report_and_its_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixtures = tmp_path / "fixtures"
    use_model(
        monkeypatch,
        RecordingLlm(
            ls.EvidenceLlm(queue=[ls.answer([moved_timetable()])]), fixtures / "flash-high"
        ),
    )
    (ok,), _, _ = await run("B3", tmp_path / "rec")
    assert ok.success
    monkeypatch.undo()

    results = tmp_path / "results"
    args = bench.build_parser().parse_args(
        [
            "--scenario", "B3", "--models", "flash", "--profiles", "high", "--repeats", "2",
            "--results-dir", str(results), "--replay", str(fixtures),
        ]
    )  # fmt: skip
    assert await bench.run_launch(args) == 0

    (report,) = results.glob("*-B3.json")
    data_ = json.loads(report.read_text())
    assert (data_["scenario"], data_["partial"], data_["matrix"]) == ("B3", False, ["flash/high"])
    assert [r["success"] for r in data_["runs"]] == [True, True]
    assert data_["runs"][0]["questions"] == 0
    assert data_["cells"][0]["medianQuestions"] == 0
    ledger = json.loads((results / "ledger.json").read_text())
    assert ledger["runs"][0]["scenario"] == "B3"
    assert ledger["totalUsd"] == pytest.approx(data_["spentUsd"], abs=1e-5)
    out = capsys.readouterr().out
    assert "вопросов 0" in out
    assert "инъекций" not in out

    readme = (results / "README.md").read_text()
    assert readme == bench.render_readme(results)
    assert "Вопросов" in readme
    assert "Токены: miss / hit / out" in readme
    assert "flash/high" in readme


def test_the_report_table_lists_why_runs_failed() -> None:
    def rec(repeat: int, ok: bool, reasons: list[str]) -> RunRecord:
        return RunRecord(
            "flash/low", "B1", "connect", repeat, ok, reasons, None, False, 12.5, 3,
            3_000, 1_000, 900, 400, 0.002, 0.25, [], 0, "status=ok", None, 1,
        )  # fmt: skip

    records = [rec(0, True, []), rec(1, False, ["расписание: записей 3 вместо 4", "второй повод"])]
    (row,) = summarize(records)
    assert (row["medianQuestions"], row["success"]) == (1, 0.5)
    table = "\n".join(
        bench._learning_table({"cells": [row], "runs": [asdict(r) for r in records]})  # pyright: ignore[reportPrivateUsage]
    )
    assert "| flash/low | 2 | 50% |" in table
    assert "6000 / 2000 / 1800 (800)" in table
    assert "- flash/low #2: расписание: записей 3 вместо 4; второй повод" in table

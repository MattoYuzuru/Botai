import { describe, expect, it } from "vitest";
import type { ActionJournalEntry, ActionPolicy, EffectivePolicy } from "@/api/actions";
import {
  actionResultText,
  actionScopeText,
  browserSteps,
  DEFAULT_MODES,
  describeActionClass,
  describeActionMode,
  describeDecision,
  describeEscalation,
  describePolicyOrigin,
  describeRequest,
  describeScopeKind,
  findExplicitPolicy,
  proposalFields,
  rollbackHint,
} from "./policy";

const policy = (patch: Partial<ActionPolicy>): ActionPolicy => ({
  id: "pol-1",
  scopeKind: "source",
  scopeId: "src-1",
  actionClass: "write_soft",
  mode: "auto",
  limits: {},
  note: null,
  updatedAt: "2026-10-31T12:00:00.000Z",
  ...patch,
});

const effective = (patch: Partial<EffectivePolicy>): EffectivePolicy => ({
  actionClass: "write_soft",
  mode: "ask",
  scopeKind: "global",
  scopeId: null,
  limits: {},
  escalated: null,
  ...patch,
});

const action = (patch: Partial<ActionJournalEntry>): ActionJournalEntry => ({
  id: 1,
  decision: "asked",
  mode: "ask",
  actionClass: "write_soft",
  scopeKind: "source",
  scopeId: "src-1",
  intent: "Записаться на пересдачу",
  proposal: {},
  request: null,
  result: null,
  rollback: null,
  idempotencyKey: null,
  runId: null,
  jobId: null,
  sourceId: "src-1",
  sourceTitle: "Личный кабинет",
  createdAt: "2026-10-31T12:00:00.000Z",
  decidedAt: null,
  executedAt: null,
  ...patch,
});

describe("словарь классов и режимов", () => {
  it("классы и режимы называются словами; неизвестное не выдаётся за известное", () => {
    expect(describeActionClass("read").label).toBe("Чтение");
    expect(describeActionClass("money").label).toBe("Деньги");
    expect(describeActionClass("future").label).toBe("Действие");
    expect(describeActionMode("auto").label).toBe("Автоматически");
    expect(describeActionMode("never").label).toBe("Никогда");
    expect(describeActionMode("future").label).toBe("Неизвестный режим");
  });

  it("дефолты — из документации: чтение сразу, остальное спрашивает", () => {
    expect(DEFAULT_MODES.read).toBe("auto");
    for (const value of ["write_soft", "write_destructive", "money", "auth"] as const) {
      expect(DEFAULT_MODES[value]).toBe("ask");
    }
  });

  it("решение: ждёт ответа только «asked»", () => {
    expect(describeDecision("asked").pending).toBe(true);
    expect(describeDecision("asked").tone).toBe("warning");
    for (const value of [
      "auto",
      "approved",
      "executing",
      "executed",
      "failed",
      "denied",
      "never",
      "expired",
    ]) {
      expect(describeDecision(value).pending, value).toBe(false);
    }
    expect(describeDecision("executed").tone).toBe("success");
    expect(describeDecision("failed").tone).toBe("danger");
    expect(describeDecision("future").label).toBe("Неизвестное решение");
  });

  it("области называются по-русски", () => {
    expect(describeScopeKind("global")).toBe("все источники");
    expect(describeScopeKind("source")).toBe("источник");
    expect(describeScopeKind("future")).toBe("область");
  });
});

describe("своя настройка или унаследованная", () => {
  it("находит явную строку только своей области", () => {
    const rows = [policy({ id: "own" }), policy({ id: "other", scopeId: "src-2" })];
    expect(findExplicitPolicy(rows, "source", "src-1", "write_soft")?.id).toBe("own");
    expect(findExplicitPolicy(rows, "source", "src-2", "write_soft")?.id).toBe("other");
    expect(findExplicitPolicy(rows, "source", "src-1", "money")).toBeNull();
    expect(
      findExplicitPolicy([policy({ scopeKind: "global", scopeId: null })], "global", null, "write_soft")?.id,
    ).toBe("pol-1");
  });

  it("подпись честно говорит, откуда режим: своя, общая, унаследованная или дефолт", () => {
    expect(describePolicyOrigin(effective({}), policy({}), [], "write_soft")).toEqual({
      label: "своя настройка",
      explicitId: "pol-1",
    });
    expect(describePolicyOrigin(undefined, null, [], "write_soft")).toEqual({
      label: "по умолчанию",
      explicitId: null,
    });
    expect(describePolicyOrigin(effective({}), null, [], "write_soft").label).toBe("по умолчанию");
    expect(
      describePolicyOrigin(
        effective({}),
        null,
        [policy({ scopeKind: "global", scopeId: null })],
        "write_soft",
      ).label,
    ).toBe("из общих настроек");
    expect(
      describePolicyOrigin(effective({ scopeKind: "space", scopeId: "space-1" }), null, [], "write_soft")
        .label,
    ).toBe("унаследовано: вуз");
    expect(
      describePolicyOrigin(
        effective({ scopeKind: "site", scopeId: "lk.fakeuni.test" }),
        null,
        [],
        "write_soft",
      ).label,
    ).toBe("унаследовано: сайт");
  });

  it("причина повышения лимитом — словами", () => {
    expect(describeEscalation("max_per_hour")).toBe("достигнут предел действий в час");
    expect(describeEscalation("max_per_day")).toBe("достигнут предел действий в день");
    expect(describeEscalation("something")).toBe("ограничение владельца");
    expect(describeEscalation(null)).toBeNull();
  });
});

describe("подпись области действия", () => {
  it("название источника важнее вида области; без названия — вид области", () => {
    expect(actionScopeText(action({ sourceTitle: "Личный кабинет" }))).toBe("Личный кабинет");
    expect(actionScopeText(action({ sourceTitle: "  " }))).toBe("источник src-1");
    expect(actionScopeText(action({ scopeKind: "global", scopeId: null, sourceTitle: null }))).toBe(
      "все источники",
    );
  });
});

describe("предложение и итог действия", () => {
  it("поля предложения — имя и значение, как их показал агент", () => {
    expect(
      proposalFields(
        action({
          proposal: {
            fields: [
              { name: "course", value: "Алгоритмы" },
              { name: "count", value: 2 },
              { name: "meta", value: { a: 1 } },
            ],
          },
        }),
      ),
    ).toEqual([
      { name: "course", value: "Алгоритмы" },
      { name: "count", value: "2" },
      { name: "meta", value: '{"a":1}' },
    ]);
    expect(proposalFields(action({ proposal: {} }))).toEqual([]);
  });

  it("запрос: метод, адрес, вид и размер тела; из proposal — если выполнения ещё не было", () => {
    expect(
      describeRequest(
        action({
          request: { method: "POST", url: "https://lk.fakeuni.test/api", body_kind: "form", body_bytes: 120 },
          proposal: { method: "GET", url: "https://ignored.test" },
        }),
      ),
    ).toEqual({
      method: "POST",
      url: "https://lk.fakeuni.test/api",
      detail: null,
      bodyKind: "form",
      bodyBytes: 120,
    });
    expect(
      describeRequest(
        action({ proposal: { method: "POST", url: "https://lk.fakeuni.test/api", body_kind: "json" } }),
      ),
    ).toMatchObject({ method: "POST", url: "https://lk.fakeuni.test/api", bodyKind: "json" });
  });

  it("итог — статус и краткая суть, а не пересказ", () => {
    expect(actionResultText(action({ result: { status: 200, summary: "Заявка принята" } }))).toBe(
      "статус 200 · Заявка принята",
    );
    expect(actionResultText(action({ result: null }))).toBeNull();
    expect(actionResultText(action({ result: { status: 500 } }))).toBe("статус 500");
  });

  it("подсказка отката и число браузерных шагов — только из данных", () => {
    expect(rollbackHint(action({ rollback: { hint: "Отменить в кабинете" } }))).toBe("Отменить в кабинете");
    expect(rollbackHint(action({ rollback: null }))).toBeNull();
    expect(browserSteps(action({ proposal: { steps: [{}, {}] } }))).toBe(2);
    expect(browserSteps(action({ proposal: { transport: "http" } }))).toBeNull();
  });
});

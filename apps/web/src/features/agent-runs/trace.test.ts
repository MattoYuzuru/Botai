import { NBSP } from "@botai/design";
import { describe, expect, it } from "vitest";
import {
  argumentRows,
  artifactsFromEvents,
  asInt,
  asRecord,
  asText,
  describeEventKind,
  describeRunStatus,
  eventCaption,
  eventError,
  eventHeadline,
  eventSummary,
  formatCostUsd,
  formatRunDuration,
  formatTokens,
  limitLabel,
  modelContent,
  runDurationMs,
  toolResultText,
} from "./trace";

const at = (iso: string) => ({ seq: 0, kind: "note", step: 0, durationMs: 0, at: iso, payload: {} });

describe("состояние хода", () => {
  it("каждое известное состояние — слово, иконка, цвет и объяснение", () => {
    for (const status of [
      "running",
      "answer",
      "paused",
      "cancelled",
      "step_limit",
      "time_limit",
      "token_limit",
      "cost_limit",
      "hard_stop",
      "error",
    ]) {
      const view = describeRunStatus(status);
      expect(view.label.length, status).toBeGreaterThan(0);
      expect(view.detail.length, status).toBeGreaterThan(0);
      expect(view.icon.length, status).toBeGreaterThan(0);
    }
    expect(describeRunStatus("running").active).toBe(true);
    expect(describeRunStatus("answer").active).toBe(false);
    expect(describeRunStatus("step_limit").tone).toBe("warning");
    expect(describeRunStatus("hard_stop").tone).toBe("danger");
  });

  it("неизвестное состояние не ломает экран и не выдаёт себя за завершённое", () => {
    const view = describeRunStatus("future_status");
    expect(view.label).toBe("Неизвестное состояние");
    expect(view.active).toBe(false);
  });
});

describe("шаги трассы", () => {
  it("вид шага — по смыслу; неизвестный вид — нейтральный", () => {
    expect(describeEventKind("model_call").label).toBe("Ответ модели");
    expect(describeEventKind("tool_result").label).toBe("Результат инструмента");
    expect(describeEventKind("future_step").label).toBe("Шаг");
  });

  it("заголовок отличает вызов инструмента и модель от просто вида", () => {
    expect(
      eventHeadline({
        seq: 1,
        kind: "tool_call",
        step: 1,
        durationMs: 0,
        at: "",
        payload: { tool: "sources_sync" },
      }),
    ).toBe("Вызов инструмента: sources_sync");
    expect(
      eventHeadline({
        seq: 2,
        kind: "limit",
        step: 1,
        durationMs: 0,
        at: "",
        payload: { limit: "steps" },
      }),
    ).toBe("Лимит: шаги");
  });

  it("подпись шага не выдумывает части, которых нет", () => {
    expect(eventCaption({ seq: 3, kind: "note", step: 4, durationMs: 0, at: "", payload: {} })).toBe("#4");
    expect(
      eventCaption({
        seq: 3,
        kind: "note",
        step: 4,
        durationMs: 65_000,
        at: "2026-10-27T11:05:00.000Z",
        payload: {},
      }),
    ).toBe(`#4 · 14:05 · 1${NBSP}мин 5${NBSP}с`);
  });

  it("лимит: причина словами, числа только из payload", () => {
    expect(limitLabel("steps")).toBe("шаги");
    expect(limitLabel("hard_stop_usd")).toBe("жёсткий лимит стоимости");
    expect(limitLabel(undefined)).toBe("неизвестный лимит");
    expect(limitLabel("future")).toBe("неизвестный лимит");
    expect(
      eventSummary({
        seq: 3,
        kind: "limit",
        step: 2,
        durationMs: 0,
        at: "",
        payload: { limit: "steps", steps: 12, total_tokens: 4_322, cost_usd: 0.0064 },
      }),
    ).toBe(`шагов: 12 · 4${NBSP}322${NBSP}токена · 0,0064${NBSP}$`);
  });

  it("ошибка шага: человеческий текст и код отдельно", () => {
    const error = eventError({
      seq: 4,
      kind: "error",
      step: 1,
      durationMs: 0,
      at: "",
      payload: { code: "llm_error", detail: "Провайдер завершился ошибкой." },
    });
    expect(error).toEqual({ code: "llm_error", detail: "Провайдер завершился ошибкой." });
    expect(eventError({ seq: 1, kind: "error", step: 0, durationMs: 0, at: "", payload: {} })).toEqual({
      code: null,
      detail: null,
    });
  });

  it("аргументы вызова — строки для карточки; вложенное — JSON", () => {
    expect(argumentRows({ url: "https://lk.fakeuni.test", deep: { a: 1 } })).toEqual([
      { name: "url", value: "https://lk.fakeuni.test" },
      { name: "deep", value: '{\n  "a": 1\n}' },
    ]);
    expect(argumentRows(null)).toEqual([]);
  });
});

describe("формат данных", () => {
  it("токены и стоимость — как их посчитал сервер", () => {
    expect(formatTokens(4_322)).toBe(`4${NBSP}322${NBSP}токена`);
    expect(formatTokens(1)).toBe(`1${NBSP}токен`);
    expect(formatCostUsd(0.0064)).toBe(`0,0064${NBSP}$`);
    expect(formatCostUsd(1.5)).toBe(`1,50${NBSP}$`);
  });

  it("длительность хода: до конца у завершённого, до «сейчас» у идущего", () => {
    expect(
      runDurationMs(
        { startedAt: "2026-10-27T11:00:00.000Z", finishedAt: "2026-10-27T11:02:05.000Z" },
        Date.parse("2026-10-27T12:00:00.000Z"),
      ),
    ).toBe(125_000);
    expect(
      runDurationMs(
        { startedAt: "2026-10-27T11:00:00.000Z", finishedAt: null },
        Date.parse("2026-10-27T11:00:10.000Z"),
      ),
    ).toBe(10_000);
    expect(runDurationMs({ startedAt: null, finishedAt: null }, 0)).toBeNull();
    expect(formatRunDuration(125_000)).toBe(`2${NBSP}мин 5${NBSP}с`);
  });

  it("артефакты хода: из результатов инструментов, без дублей", () => {
    const events = [
      {
        seq: 1,
        kind: "tool_result",
        step: 1,
        durationMs: 0,
        at: "",
        payload: {
          result: {
            artifacts: [
              { id: "a1", kind: "page", bytes: 1024, url: "https://lk.fakeuni.test/schedule" },
              { id: "a1", kind: "page", bytes: 1024, url: null },
            ],
          },
        },
      },
      {
        seq: 2,
        kind: "model_call",
        step: 2,
        durationMs: 0,
        at: "",
        payload: {},
      },
    ];
    expect(artifactsFromEvents(events)).toEqual([
      { id: "a1", kind: "page", bytes: 1024, url: "https://lk.fakeuni.test/schedule" },
    ]);
    expect(artifactsFromEvents([])).toEqual([]);
  });

  it("текст модели и результат инструмента читаются, только если сервер их дал", () => {
    expect(
      modelContent({
        seq: 1,
        kind: "model_call",
        step: 1,
        durationMs: 0,
        at: "",
        payload: { content: "Ответ" },
      }),
    ).toBe("Ответ");
    expect(
      modelContent({ seq: 1, kind: "model_call", step: 1, durationMs: 0, at: "", payload: {} }),
    ).toBeNull();
    expect(
      toolResultText({
        seq: 1,
        kind: "tool_result",
        step: 1,
        durationMs: 0,
        at: "",
        payload: { result: { ok: true } },
      }),
    ).toBe('{\n  "ok": true\n}');
  });

  it("payload'ы читаются безопасно: мусор не выдаётся за данные", () => {
    expect(asRecord([1])).toBeNull();
    expect(asRecord(null)).toBeNull();
    expect(asText("  ")).toBeNull();
    expect(asInt(1.9)).toBe(1);
    expect(asInt("1")).toBeNull();
  });
});

describe("время шага", () => {
  it("время — в поясе приложения", () => {
    expect(eventCaption(at("2026-10-27T08:05:00.000Z"))).toContain("11:05");
  });
});

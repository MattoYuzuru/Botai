import { describe, expect, it } from "vitest";
import { answerFits } from "./handlers";

const choice = (extra: object = {}) => ({
  type: "choice",
  mode: "single",
  required: true,
  allowOther: false,
  options: [{ value: "a" }, { value: "b" }],
  ...extra,
});

describe("форма ответа, как её проверяет сервер", () => {
  it("одиночный выбор: ровно одно значение из списка; «Другое» — только если разрешено", () => {
    expect(answerFits(choice(), "a")).toBe(true);
    expect(answerFits(choice(), ["a"])).toBe(true);
    expect(answerFits(choice(), ["a", "b"])).toBe(false);
    expect(answerFits(choice(), "z")).toBe(false);
    expect(answerFits(choice({ allowOther: true }), "своё")).toBe(true);
    expect(answerFits(choice({ allowOther: true }), "x".repeat(501))).toBe(false);
    expect(answerFits(choice(), null)).toBe(false);
  });

  it("множественный выбор: необязательный принимает пустой список, обязательный — нет", () => {
    expect(answerFits(choice({ mode: "multi", required: false }), [])).toBe(true);
    expect(answerFits(choice({ mode: "multi", required: true }), [])).toBe(false);
    expect(answerFits(choice({ mode: "multi" }), ["a", "b"])).toBe(true);
  });

  it("ввод: строка не длиннее maxLength; пустая только у необязательного не секретного", () => {
    const input = { type: "input", required: false, secret: false, maxLength: 5 };
    expect(answerFits(input, "")).toBe(true);
    expect(answerFits(input, "123456")).toBe(false);
    expect(answerFits({ ...input, required: true }, "")).toBe(false);
    expect(answerFits({ ...input, secret: true }, "")).toBe(false);
    expect(answerFits(input, null)).toBe(false);
    expect(answerFits(input, ["a"])).toBe(false);
  });

  it("подтверждение — только булево значение; остальные блоки ответа не принимают", () => {
    expect(answerFits({ type: "confirm" }, true)).toBe(true);
    expect(answerFits({ type: "approval" }, "yes")).toBe(false);
    expect(answerFits({ type: "text" }, true)).toBe(false);
  });
});

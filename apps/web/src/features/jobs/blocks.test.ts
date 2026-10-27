import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { isQuestion, KNOWN_TYPES, parseBlock, patchBlock } from "./blocks";

const root = resolve(import.meta.dirname, "../../../../..");
const valid = JSON.parse(
  readFileSync(resolve(root, "contracts/examples/valid/agent-ui.blocks.json"), "utf8"),
) as unknown[];
const invalid = JSON.parse(
  readFileSync(resolve(root, "contracts/examples/invalid/agent-ui.blocks.json"), "utf8"),
) as unknown[];

describe("блоки из контракта", () => {
  it("все валидные примеры контракта разбираются как известные типы", () => {
    for (const raw of valid) {
      const block = parseBlock(raw);
      expect(block?.type, JSON.stringify(raw).slice(0, 60)).not.toBe("unknown");
      expect(KNOWN_TYPES).toContain(block?.type);
    }
    expect(new Set(valid.map((raw) => (raw as { type: string }).type)).size).toBeGreaterThanOrEqual(7);
  });

  it("невалидные примеры контракта не рисуются как известные блоки", () => {
    for (const raw of invalid) {
      expect(parseBlock(raw)?.type, JSON.stringify(raw).slice(0, 80)).toBe("unknown");
    }
  });
});

const base = { id: "01JAAAAAAAAAAAAAAAAAAAAAA1", at: "2026-10-27T10:00:00Z" };

describe("parseBlock", () => {
  it("не объект — null", () => {
    for (const raw of [null, 1, "x", [], undefined]) expect(parseBlock(raw)).toBeNull();
  });

  it("неизвестный тип — UnknownBlock с читаемым текстом, без разметки", () => {
    const block = parseBlock({ ...base, type: "carousel", markdown: "Смотри <b>это</b>", images: ["x"] });
    expect(block).toMatchObject({ type: "unknown", rawType: "carousel", text: "Смотри <b>это</b>" });
  });

  it("известный тип с битой формой тоже становится UnknownBlock", () => {
    expect(
      parseBlock({
        ...base,
        type: "choice",
        question: "?",
        mode: "single",
        required: true,
        options: [{ value: "a", label: "A" }],
      })?.type,
    ).toBe("unknown");
    expect(
      parseBlock({ ...base, type: "input", question: "?", inputKind: "telepathy", required: true })?.type,
    ).toBe("unknown");
    expect(parseBlock({ type: "text", markdown: "без id", at: base.at })?.type).toBe("unknown");
    expect(parseBlock({ ...base, id: "not-a-ulid", type: "text", markdown: "x" })?.type).toBe("unknown");
  });

  it("блок без текстовых полей получает text=null", () => {
    expect(parseBlock({ ...base, type: "mystery", blob: 1 })).toMatchObject({ type: "unknown", text: null });
  });

  it("длинный текст неизвестного блока обрезается", () => {
    const block = parseBlock({ ...base, type: "mystery", text: "а".repeat(5000) });
    expect(block?.type === "unknown" && block.text?.length).toBe(2000);
  });
});

describe("block_update", () => {
  it("патч меняет поле, но не тип и не id", () => {
    const thinking = parseBlock({ ...base, type: "thinking", stage: "Читаю", state: "running" });
    if (!thinking) throw new Error("нет блока");
    const updated = patchBlock(thinking, { state: "ok", type: "text", id: "01JZZZZZZZZZZZZZZZZZZZZZZZ" });
    expect(updated).toMatchObject({ type: "thinking", id: base.id, state: "ok", stage: "Читаю" });
  });

  it("патч, ломающий форму, превращает блок в неизвестный, а не роняет приложение", () => {
    const thinking = parseBlock({ ...base, type: "thinking", stage: "Читаю" });
    if (!thinking) throw new Error("нет блока");
    expect(patchBlock(thinking, { stage: 42 }).type).toBe("unknown");
  });

  it("не объект в патче — блок без изменений", () => {
    const thinking = parseBlock({ ...base, type: "thinking", stage: "Читаю" });
    if (!thinking) throw new Error("нет блока");
    expect(patchBlock(thinking, "oops")).toBe(thinking);
  });
});

describe("вопросы", () => {
  it("вопрос — choice, input, confirm, approval", () => {
    const sample = (type: string) => ({ type, id: base.id, rawType: type, at: null, text: null }) as never;
    expect(isQuestion(sample("choice"))).toBe(true);
    expect(isQuestion(sample("approval"))).toBe(true);
    expect(isQuestion(sample("text"))).toBe(false);
    expect(isQuestion(sample("wait"))).toBe(false);
  });
});

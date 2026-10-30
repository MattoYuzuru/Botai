import { describe, expect, it } from "vitest";
import { plainText, QUOTE_MAX, quoteOf } from "./quote";

describe("цитата вопроса для ответа человека", () => {
  it("markdown сводится к простому тексту: без разметки, ссылок, кода и таблиц", () => {
    const markdown = [
      "## Итог",
      "",
      "| День | Пар |",
      "|:---|---:|",
      "| Пн | 3 |",
      "",
      "- **Алгоритмы** в `В-310`",
      "- [в кабинете](https://lk.test/x) и *курсив*",
      "",
      "```chart",
      '{"type":"bar"}',
      "```",
    ].join("\n");
    expect(plainText(markdown)).toBe("Итог Алгоритмы в В-310 в кабинете и курсив");
  });

  it("цитируется вопрос: последняя фраза со знаком вопроса в последнем абзаце", () => {
    expect(quoteOf("Нашла два варианта.\n\nЯ могу взять оба. Какой вариант выбираете?")).toEqual({
      text: "Какой вариант выбираете?",
    });
    expect(quoteOf("Вышка — это **НИУ ВШЭ**?", "01JMKCDB000000000000000001")).toEqual({
      text: "Вышка — это НИУ ВШЭ?",
      blockId: "01JMKCDB000000000000000001",
    });
  });

  it("нет вопроса — последняя фраза; пустой текст цитаты не даёт", () => {
    expect(quoteOf("Расписание обновлено. Подробнее — в кабинете.")?.text).toBe("Подробнее — в кабинете.");
    expect(quoteOf("   ")).toBeNull();
    expect(quoteOf("```chart\n{}\n```")).toBeNull();
  });

  it("длинное режется до QUOTE_MAX с многоточием", () => {
    const quote = quoteOf(`Что ${"очень ".repeat(80)}важно?`);
    expect(quote?.text.length).toBeLessThanOrEqual(QUOTE_MAX);
    expect(quote?.text.endsWith("…")).toBe(true);
  });
});

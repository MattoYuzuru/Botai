import { describe, expect, it } from "vitest";
import { resultConfidence, textConfidence } from "./confidence";
import { internalHref, internalIcon, internalLabel, parseInternalLink } from "./internal-links";
import { buildSuggestions } from "./suggestions";

const NOW = Date.UTC(2026, 9, 27, 11, 5);
const HOUR = 3_600_000;
const iso = (ms: number) => new Date(ms).toISOString();

describe("внутренние ссылки агента", () => {
  it("botai://threads, documents, connections и attachments — внутренние", () => {
    const thread = parseInternalLink("botai://threads/01JMKCDB000");
    expect(thread).toEqual({ kind: "thread", id: "01JMKCDB000" });
    expect(internalHref(thread as never)).toBe("/chat?thread=01JMKCDB000");
    expect(internalLabel(thread as never)).toBe("Открыть чат");
    expect(internalIcon(thread as never)).toBe("chat");
    const doc = parseInternalLink("botai://documents/01JMKCDB000");
    expect(doc).toEqual({ kind: "document", id: "01JMKCDB000" });
    expect(internalHref(doc as never)).toBe("/study");
    expect(internalLabel(doc as never)).toContain("документ");
    expect(internalIcon(doc as never)).toBe("document");
    const conn = parseInternalLink("botai://connections/abc_DEF-1");
    expect(conn).toEqual({ kind: "connection", id: "abc_DEF-1" });
    expect(internalHref(conn as never)).toBe("/study");
    expect(internalIcon(conn as never)).toBe("connection");
    const file = parseInternalLink("botai://attachments/A1");
    expect(file).toEqual({ kind: "attachment", id: "A1" });
    expect(internalHref(file as never)).toBe("/api/v1/attachments/A1/content");
    expect(internalIcon(file as never)).toBe("attach");
  });

  it("всё остальное не внутренняя ссылка: сайты, botai://sources/N, чужие схемы, обход пути, мусор", () => {
    for (const url of [
      "https://lk.fakeuni.test/timetable",
      "botai://sources/3",
      "botai://documents/",
      "botai://threads/../../etc",
      "botai://documents/a/b",
      "botai://attachments/a?x=1",
      "javascript:alert(1)",
      "botai://other/1",
      "",
      null,
      undefined,
    ]) {
      expect(parseInternalLink(url as string), String(url)).toBeNull();
    }
  });

  it("адрес тред — /chat?thread=, остальные — «Учёба»; лишнее в идентификаторе не пролезает в адрес", () => {
    expect(internalHref({ kind: "thread", id: "a-b_c" })).toBe("/chat?thread=a-b_c");
    expect(internalHref({ kind: "document", id: "a-b_c" })).toBe("/study");
    expect(internalHref({ kind: "connection", id: "x y/z" })).toBe("/study");
  });
});

describe("честное «Не уверен»", () => {
  it("«Не знаю: …» и «Ответ может быть неполным» — не уверен", () => {
    expect(textConfidence("Не знаю: в подключённых документах нет ответа.")).toBe("unsure");
    expect(textConfidence("  не знаю: нет данных")).toBe("unsure");
    expect(textConfidence("Пары есть.\n\nОтвет может быть неполным: я остановился по лимиту шагов.")).toBe(
      "unsure",
    );
  });

  it("обычный ответ не помечается, а «не знаю» в середине фразы — тоже", () => {
    expect(textConfidence("Следующая пара в 9:30 [1].")).toBe("ok");
    expect(textConfidence("Я не знаю никого, кто бы…")).toBe("ok");
    expect(textConfidence("")).toBe("ok");
  });

  it("итог задания: остановился не ответом — неполный", () => {
    expect(resultConfidence({ stopped: "budget" })).toBe("unsure");
    expect(resultConfidence({ stopped: "quota" })).toBe("unsure");
    expect(resultConfidence({ stopped: "answer" })).toBe("ok");
    expect(resultConfidence(null)).toBe("ok");
    expect(resultConfidence({})).toBe("ok");
  });
});

describe("подсказки пустого чата", () => {
  const base = { nowMs: NOW, events: [], deadlines: [], documentTitles: [], needsAttention: [] };

  it("нет данных — нет подсказок", () => {
    expect(buildSuggestions(base)).toEqual([]);
  });

  it("пара впереди — «следующая пара»; все пары позади — другой вопрос", () => {
    const ahead = [{ startsAt: iso(NOW + HOUR), endsAt: iso(NOW + 2 * HOUR) }];
    expect(buildSuggestions({ ...base, events: ahead })).toEqual(["Какая следующая пара?"]);
    const behind = [{ startsAt: iso(NOW - 3 * HOUR), endsAt: iso(NOW - 2 * HOUR) }];
    expect(buildSuggestions({ ...base, events: behind })).toEqual(["Что сегодня было по расписанию?"]);
  });

  it("работы: просрочено важнее недели, неделя важнее общего вопроса", () => {
    const week = { title: "Лаба", dueMs: NOW + 3 * 24 * HOUR };
    const overdue = { title: "Эссе", dueMs: NOW - HOUR };
    const far = { title: "Проект", dueMs: NOW + 30 * 24 * HOUR };
    const none = { title: "Чтение", dueMs: null };
    expect(buildSuggestions({ ...base, deadlines: [week, overdue] })).toEqual(["Что у меня просрочено?"]);
    expect(buildSuggestions({ ...base, deadlines: [week, far] })).toEqual(["Что сдавать на неделе?"]);
    expect(buildSuggestions({ ...base, deadlines: [far] })).toEqual(["Какие у меня дедлайны?"]);
    expect(buildSuggestions({ ...base, deadlines: [none] })).toEqual(["Какие у меня дедлайны?"]);
  });

  it("не больше трёх, названия документа и вуза — настоящие, длинное название сокращается", () => {
    const result = buildSuggestions({
      nowMs: NOW,
      events: [{ startsAt: iso(NOW + HOUR), endsAt: iso(NOW + 2 * HOUR) }],
      deadlines: [{ title: "Лаба", dueMs: NOW + 2 * HOUR }],
      documentTitles: ["Положение о промежуточной аттестации студентов очной формы обучения"],
      needsAttention: ["ЦУ"],
    });
    expect(result).toHaveLength(3);
    expect(result[2]).toBe("О чём документ «Положение о промежуточной аттестаци…»?");
    const two = buildSuggestions({ ...base, documentTitles: ["Правила"], needsAttention: ["ЦУ"] });
    expect(two).toEqual(["О чём документ «Правила»?", "Почему не обновляется ЦУ?"]);
  });
});

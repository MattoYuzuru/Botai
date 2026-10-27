import { describe, expect, it } from "vitest";
import type { Message } from "@/api/types";
import { initialJob, type JobSnapshot } from "@/features/jobs/job-state";
import { resultConfidence, textConfidence } from "./confidence";
import { internalHref, internalIcon, internalLabel, parseInternalLink } from "./internal-links";
import type { OutboxItem } from "./outbox";
import { buildSuggestions } from "./suggestions";
import { awaitingReply, buildTimeline, failureText, isReplying } from "./timeline";

const NOW = Date.UTC(2026, 9, 27, 11, 5);
const HOUR = 3_600_000;
const ULID = (n: number) => `01JMKCDB00000000000000${String(n).padStart(4, "0")}`;
const iso = (ms: number) => new Date(ms).toISOString();

describe("внутренние ссылки источников", () => {
  it("botai://documents и botai://connections — внутренние, ведут в «Учёбу»", () => {
    const doc = parseInternalLink("botai://documents/01JMKCDB000");
    expect(doc).toEqual({ kind: "document", id: "01JMKCDB000" });
    expect(internalHref(doc as never)).toBe("/study?document=01JMKCDB000");
    expect(internalLabel(doc as never)).toContain("документ");
    expect(internalIcon(doc as never)).toBe("document");
    const conn = parseInternalLink("botai://connections/abc_DEF-1");
    expect(conn).toEqual({ kind: "connection", id: "abc_DEF-1" });
    expect(internalHref(conn as never)).toBe("/study?connection=abc_DEF-1");
    expect(internalIcon(conn as never)).toBe("connection");
  });

  it("всё остальное не внутренняя ссылка: сайты, botai://sources/N, чужие схемы, обход пути, мусор", () => {
    for (const url of [
      "https://lk.fakeuni.test/timetable",
      "botai://sources/3",
      "botai://documents/",
      "botai://documents/../../etc",
      "botai://documents/a/b",
      "botai://documents/a?x=1",
      "javascript:alert(1)",
      "botai://other/1",
      "",
      null,
      undefined,
    ]) {
      expect(parseInternalLink(url as string), String(url)).toBeNull();
    }
  });

  it("идентификатор в адресе экранируется", () => {
    expect(internalHref({ kind: "document", id: "a-b_c" })).toBe("/study?document=a-b_c");
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

function message(partial: Partial<Message> & Pick<Message, "id" | "role">): Message {
  return { blocks: [], jobId: null, createdAt: iso(NOW), ...partial };
}
const textBlock = (id: string, markdown: string) => ({ type: "text", id, at: iso(NOW), markdown });

function pending(partial: Partial<OutboxItem> = {}): OutboxItem {
  return {
    id: "p1",
    ownerId: "u1",
    threadId: "t1",
    spaceId: null,
    text: "Ещё не ушло",
    createdAt: NOW,
    status: "queued",
    attempts: 0,
    retryAt: null,
    maybeSent: false,
    ...partial,
  };
}

function job(extra: Partial<JobSnapshot> = {}): JobSnapshot {
  return { ...initialJob("job-1"), state: "running", startedAt: NOW - 5_000, ...extra };
}

describe("лента чата", () => {
  const history: Message[] = [
    message({
      id: ULID(1),
      role: "user",
      blocks: [textBlock(ULID(10), "Какая следующая пара?")],
      createdAt: iso(NOW - 60_000),
    }),
    message({
      id: ULID(2),
      role: "agent",
      jobId: "job-0",
      blocks: [textBlock(ULID(11), "Алгоритмы в 9:30.")],
      createdAt: iso(NOW - 50_000),
    }),
  ];

  it("история по порядку, затем ожидающие отправки с подписью состояния", () => {
    const timeline = buildTimeline({
      messages: history,
      pending: [pending({ status: "queued" }), pending({ id: "p2", status: "sending", text: "Второе" })],
      job: null,
      nowMs: NOW,
    });
    expect(timeline.map((item) => item.kind)).toEqual(["user", "agent", "user", "user"]);
    expect(timeline[0]).toMatchObject({ kind: "user", text: "Какая следующая пара?", status: "sent" });
    expect(timeline[1]).toMatchObject({ kind: "agent", jobId: "job-0" });
    expect(timeline[2]).toMatchObject({ status: "queued", text: "Ещё не ушло" });
    expect(timeline[3]).toMatchObject({ status: "sending" });
  });

  it("живой ответ: шаги и блоки приходят из потока; пока ничего нет — честное «Жду ответ агента»", () => {
    const empty = buildTimeline({ messages: history, pending: [], job: job(), nowMs: NOW });
    const live = empty.at(-1);
    expect(live).toMatchObject({ kind: "live", running: true, failure: null });
    expect(live && live.kind === "live" && live.steps.map((step) => step.label)).toEqual([
      "Жду ответ агента",
    ]);

    const withSteps = buildTimeline({
      messages: history,
      pending: [],
      job: job({
        blocks: [
          {
            type: "thinking",
            id: ULID(20),
            at: iso(NOW - 4_000),
            stage: "Читаю расписание",
            state: "running",
          },
        ],
      }),
      nowMs: NOW,
    }).at(-1);
    expect(withSteps && withSteps.kind === "live" && withSteps.steps.map((step) => step.label)).toEqual([
      "Читаю расписание",
    ]);
  });

  it("когда ответ уже в истории, живой ответ не дублируется", () => {
    const timeline = buildTimeline({
      messages: history,
      pending: [],
      job: job({ jobId: "job-0", state: "done" }),
      nowMs: NOW,
    });
    expect(timeline.some((item) => item.kind === "live")).toBe(false);
  });

  it("сбой: человеческий текст без кода; отмена — «Ответ остановлен»", () => {
    const failed = job({ state: "failed", problem: { code: "quota_exceeded", detail: "", extra: {} } });
    expect(failureText(failed)).toBe("На сегодня лимит ответов исчерпан. Он обновится завтра.");
    expect(failureText(failed)).not.toMatch(/quota|_/);
    expect(failureText(job({ state: "cancelled" }))).toBe("Ответ остановлен.");
    expect(failureText(job({ state: "failed", problem: null }))).toMatch(/Не удалось/);
    const live = buildTimeline({ messages: history, pending: [], job: failed, nowMs: NOW }).at(-1);
    expect(live).toMatchObject({ kind: "live", running: false, failure: expect.any(String) });
  });

  it("итог «остановился по лимиту» помечает живой ответ как неуверенный", () => {
    const live = buildTimeline({
      messages: history,
      pending: [],
      job: job({ state: "done", result: { stopped: "budget" } }),
      nowMs: NOW,
    }).at(-1);
    expect(live).toMatchObject({ kind: "live", unsure: true });
  });

  it("«Думал · N с» замирает: время окончания берётся из последнего блока", () => {
    const live = buildTimeline({
      messages: history,
      pending: [],
      job: job({
        state: "done",
        blocks: [
          { type: "thinking", id: ULID(21), at: iso(NOW - 9_000), stage: "Читаю", state: "ok" },
          { type: "text", id: ULID(22), at: iso(NOW - 2_000), markdown: "Готово" },
        ],
      }),
      nowMs: NOW + 60_000,
    }).at(-1);
    expect(live && live.kind === "live" && live.finishedAt).toBe(NOW - 2_000);
  });
});

describe("ожидание ответа без известного задания", () => {
  const ask = message({
    id: ULID(3),
    role: "user",
    blocks: [textBlock(ULID(12), "Вопрос")],
    createdAt: iso(NOW - 30_000),
  });

  it("свежий вопрос без очереди — ответ ещё может прийти; старый или с очередью — нет", () => {
    expect(awaitingReply([ask], [], NOW)).toBe(true);
    expect(awaitingReply([ask], [], NOW + 10 * 60_000)).toBe(false);
    expect(awaitingReply([ask], [pending()], NOW)).toBe(false);
    expect(awaitingReply([], [], NOW)).toBe(false);
    const answered = message({ id: ULID(4), role: "agent", createdAt: iso(NOW - 10_000) });
    expect(awaitingReply([ask, answered], [], NOW)).toBe(false);
  });

  it("такой вопрос показывает «Жду ответ агента» и блокирует новое сообщение", () => {
    const timeline = buildTimeline({ messages: [ask], pending: [], job: null, nowMs: NOW });
    expect(timeline.at(-1)).toMatchObject({ kind: "live", jobId: null, running: true });
    expect(isReplying(null, true)).toBe(true);
    expect(isReplying(null, false)).toBe(false);
    expect(isReplying(job(), false)).toBe(true);
    expect(isReplying(job({ state: "done" }), true)).toBe(false);
  });
});

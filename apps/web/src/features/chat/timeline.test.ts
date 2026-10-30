import { describe, expect, it } from "vitest";
import type { Message } from "@/api/types";
import type { JobOut, ThreadRuntime } from "@/api/v3";
import { initialJob, type JobSnapshot } from "@/features/jobs/job-state";
import { composerState } from "./composer-state";
import type { OutboxItem } from "./outbox";
import {
  awaitingReply,
  buildTimeline,
  failureText,
  mergeJob,
  runtimeJobs,
  type ThreadJob,
  type Turn,
  userText,
} from "./timeline";
import { pendingEditors } from "./timeline-view";
import { unsavedBlocks } from "./use-thread-jobs";

const NOW = Date.UTC(2026, 9, 27, 11, 5);
const ULID = (n: number) => `01JMKCDB00000000000000${String(n).padStart(4, "0")}`;
const iso = (ms: number) => new Date(ms).toISOString();

function message(partial: Partial<Message> & Pick<Message, "id" | "role">): Message {
  return { blocks: [], jobId: null, createdAt: iso(NOW), ...partial };
}
const textBlock = (id: string, markdown: string, extra: object = {}) => ({
  type: "text",
  id,
  at: iso(NOW),
  markdown,
  ...extra,
});

function pending(partial: Partial<OutboxItem> = {}): OutboxItem {
  return {
    id: "p1",
    ownerId: "u1",
    threadId: "t1",
    spaceId: null,
    text: "Ещё не ушло",
    attachments: [],
    createdAt: NOW,
    status: "queued",
    attempts: 0,
    retryAt: null,
    maybeSent: false,
    ...partial,
  };
}

function snapshot(id: string, extra: Partial<JobSnapshot> = {}): JobSnapshot {
  return { ...initialJob(id), state: "running", startedAt: NOW - 5_000, ...extra };
}
const job = (
  id: string,
  kind: string,
  extra: Partial<JobSnapshot> = {},
  connectionId: string | null = null,
): ThreadJob => ({
  id,
  kind,
  connectionId,
  snapshot: snapshot(id, extra),
});

const kinds = (turns: readonly Turn[]) => turns.map((turn) => turn.kind);
const build = (input: Partial<Parameters<typeof buildTimeline>[0]>) =>
  buildTimeline({ messages: [], pending: [], jobs: [], nowMs: NOW, ...input });

describe("один таймлайн треда", () => {
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
      jobId: "chat-0",
      blocks: [textBlock(ULID(11), "Алгоритмы в 9:30.")],
      createdAt: iso(NOW - 50_000),
    }),
  ];

  it("история по порядку, затем ожидающие отправки с подписью состояния", () => {
    const { turns } = build({
      messages: history,
      pending: [pending({ status: "queued" }), pending({ id: "p2", status: "sending", text: "Второе" })],
    });
    expect(kinds(turns)).toEqual(["user", "block", "user", "user"]);
    expect(turns[0]).toMatchObject({ kind: "user", text: "Какая следующая пара?", status: "sent" });
    expect(turns[1]).toMatchObject({ kind: "block", jobId: "chat-0" });
    expect(turns[2]).toMatchObject({ status: "queued", text: "Ещё не ушло" });
    expect(turns[3]).toMatchObject({ status: "sending" });
  });

  it("вложения сообщения человека — из блоков attachment; ожидающее отправки несёт свои вложения", () => {
    const file = {
      type: "attachment",
      id: ULID(30),
      at: iso(NOW),
      attachmentId: "A1",
      name: "фото.png",
      mime: "image/png",
      size: 10,
      kind: "image",
    };
    const { turns } = build({
      messages: [message({ id: ULID(3), role: "user", blocks: [file, textBlock(ULID(31), "Что на фото?")] })],
      pending: [
        pending({
          text: "",
          attachments: [{ id: "A2", name: "голос.webm", mime: "audio/webm", size: 5, kind: "audio" }],
        }),
      ],
    });
    expect(turns[0]).toMatchObject({ kind: "user", text: "Что на фото?" });
    expect(turns[0]?.kind === "user" && turns[0].attachments.map((item) => item.name)).toEqual(["фото.png"]);
    expect(turns[1]?.kind === "user" && turns[1].attachments.map((item) => item.name)).toEqual([
      "голос.webm",
    ]);
  });

  it("задача и ход агента в одной ленте: блоки задачи рисуются на месте, шаги собраны в один блок", () => {
    const connect = "connect-1";
    const messages = [
      ...history,
      message({
        id: ULID(4),
        role: "agent",
        jobId: connect,
        blocks: [
          { type: "thinking", id: ULID(40), at: iso(NOW - 40_000), stage: "Ищу личный кабинет", state: "ok" },
          {
            type: "input",
            id: ULID(41),
            at: iso(NOW - 30_000),
            question: "Логин",
            inputKind: "text",
            required: true,
          },
        ],
        createdAt: iso(NOW - 30_000),
      }),
    ];
    const live = job(connect, "connect", {
      state: "waiting_user",
      blocks: [
        { type: "thinking", id: ULID(40), at: iso(NOW - 40_000), stage: "Ищу личный кабинет", state: "ok" },
        {
          type: "thinking",
          id: ULID(42),
          at: iso(NOW - 20_000),
          stage: "Читаю список курсов",
          state: "running",
        },
      ],
    });
    const { turns } = build({ messages, jobs: [live] });
    // Задача ждёт человека: её «Ждёт ответа» стоит последним, под всем, что появилось за время работы.
    expect(kinds(turns)).toEqual(["user", "block", "block", "thinking"]);
    const thinking = turns.find((turn) => turn.kind === "thinking");
    expect(thinking?.kind === "thinking" && thinking.steps.map((step) => step.label)).toEqual([
      "Ищу личный кабинет",
      "Читаю список курсов",
    ]);
    // Последний бегущий шаг при ожидании человека — «ждёт», это слышно словами.
    expect(thinking?.kind === "thinking" && thinking.steps.at(-1)?.state).toBe("waiting");
    expect(turns.at(-1)).toMatchObject({ kind: "thinking", jobId: connect });
    expect(turns.at(-2)).toMatchObject({ kind: "block", jobId: connect });
  });

  it("живой блок заменяет сохранённый на месте: план обновляется тем же id, а не дублируется", () => {
    const plan = (state: string) => ({
      type: "plan",
      id: ULID(50),
      at: iso(NOW),
      title: "План",
      steps: [{ id: "s1", title: "Шаг", state }],
    });
    const messages = [message({ id: ULID(5), role: "agent", jobId: "chat-1", blocks: [plan("active")] })];
    const { turns } = build({
      messages,
      jobs: [job("chat-1", "chat", { blocks: [plan("done") as never] })],
    });
    const plans = turns.filter((turn) => turn.kind === "block" && turn.block.type === "plan");
    expect(plans).toHaveLength(1);
    expect(
      plans[0]?.kind === "block" && plans[0].block.type === "plan" && plans[0].block.steps[0]?.state,
    ).toBe("done");
  });

  it("задание идёт, шагов нет — честное «Жду ответ агента», а не выдуманные шаги", () => {
    const { turns } = build({ messages: history, jobs: [job("chat-2", "chat")] });
    const last = turns.at(-1);
    expect(last).toMatchObject({ kind: "thinking", running: true });
    expect(last?.kind === "thinking" && last.steps.map((step) => step.label)).toEqual(["Жду ответ агента"]);
  });

  it("сбой: человеческий текст без кода; у задачи с источником есть «Войти заново»", () => {
    const failed = snapshot("chat-3", {
      state: "failed",
      problem: { code: "quota_exceeded", detail: "", extra: {} },
    });
    expect(failureText(failed)).toBe("На сегодня лимит ответов исчерпан. Он обновится завтра.");
    expect(failureText(failed)).not.toMatch(/quota|_/);
    expect(failureText(snapshot("x", { state: "failed", problem: null }))).toMatch(/Не удалось/);
    expect(failureText(snapshot("x", { state: "cancelled" }))).toMatch(/Остановлено/);
    expect(
      failureText(
        snapshot("x", { state: "failed", problem: { code: "login_failed", detail: "", extra: {} } }),
      ),
    ).toBe("Вход не завершён. Можно войти заново.");

    const { turns } = build({
      messages: history,
      jobs: [
        { id: "chat-3", kind: "chat", connectionId: null, snapshot: failed },
        job(
          "c1",
          "connect",
          { state: "failed", problem: { code: "source_unavailable", detail: "", extra: {} } },
          "CONN",
        ),
      ],
    });
    const failures = turns.filter((turn) => turn.kind === "failure");
    expect(failures.map((turn) => turn.kind === "failure" && turn.connectionId)).toEqual([null, "CONN"]);
  });

  it("«Думал · N с» замирает: время окончания берётся из последнего блока", () => {
    const { turns } = build({
      messages: history,
      jobs: [
        job("chat-4", "chat", {
          state: "done",
          blocks: [
            { type: "thinking", id: ULID(21), at: iso(NOW - 9_000), stage: "Читаю", state: "ok" },
            { type: "text", id: ULID(22), at: iso(NOW - 2_000), markdown: "Готово" },
          ],
        }),
      ],
      nowMs: NOW + 60_000,
    });
    const thinking = turns.find((turn) => turn.kind === "thinking");
    expect(thinking?.kind === "thinking" && thinking.finishedAt).toBe(NOW - 2_000);
    expect(thinking?.kind === "thinking" && thinking.running).toBe(false);
  });

  it("быстрые ответы — только у последнего текста агента, пока ничего не идёт", () => {
    const answer = message({
      id: ULID(6),
      role: "agent",
      jobId: "chat-5",
      blocks: [textBlock(ULID(60), "Готово.", { quickReplies: ["Да", "Нет", "", "x".repeat(80)] })],
    });
    const done = build({ messages: [answer] });
    // Кнопки несут цитату того, на что отвечают: ответ «Да» без неё в ленте непонятен.
    expect(done.quickReplies).toEqual({
      key: `block:chat-5:${ULID(60)}`,
      options: ["Да", "Нет"],
      quote: { text: "Готово.", blockId: ULID(60) },
    });
    // Дальше написал человек (ожидает отправки), идёт ход агента — кнопок нет.
    expect(build({ messages: [answer], pending: [pending()] }).quickReplies).toBeNull();
    expect(build({ messages: [answer], jobs: [job("chat-6", "chat")] }).quickReplies).toBeNull();
    // После ответа агента уже написал человек: кнопки под старым ответом не нужны.
    expect(build({ messages: [answer, message({ id: ULID(7), role: "user" })] }).quickReplies).toBeNull();
  });
});

describe("мышление: идёт — внизу, закончилось — над итоговым ответом", () => {
  const thought = (id: number, at: number, stage: string, state = "ok") => ({
    type: "thinking",
    id: ULID(id),
    at: iso(at),
    stage,
    state,
  });
  const say = (id: number, at: number, markdown: string) => textBlock(ULID(id), markdown, { at: iso(at) });
  const agent = (n: number, jobId: string, at: number, ...blocks: object[]) =>
    message({ id: ULID(n), role: "agent", jobId, blocks, createdAt: iso(at) });
  const history: Message[] = [
    message({
      id: ULID(90),
      role: "user",
      blocks: [textBlock(ULID(91), "Какая следующая пара?")],
      createdAt: iso(NOW - 600_000),
    }),
    message({
      id: ULID(92),
      role: "agent",
      jobId: "chat-0",
      blocks: [textBlock(ULID(93), "Алгоритмы в 9:30.")],
      createdAt: iso(NOW - 590_000),
    }),
  ];

  it("пока задача работает, живое «Думает» последнее: сообщения задачи появляются над ним", () => {
    const task = "connect-1";
    const messages = [
      message({
        id: ULID(1),
        role: "user",
        blocks: [textBlock(ULID(10), "Подключи ЦУ")],
        createdAt: iso(NOW - 90_000),
      }),
      agent(2, task, NOW - 80_000, thought(20, NOW - 80_000, "Ищу личный кабинет")),
      agent(3, task, NOW - 60_000, say(21, NOW - 60_000, "Нашла вход, захожу.")),
      agent(4, task, NOW - 40_000, thought(22, NOW - 40_000, "Читаю расписание", "running")),
      agent(5, task, NOW - 20_000, say(23, NOW - 20_000, "Нужно подтвердить вход в приложении.")),
    ];
    const live = job(task, "connect", {
      state: "running",
      blocks: [
        thought(20, NOW - 80_000, "Ищу личный кабинет") as never,
        thought(22, NOW - 40_000, "Читаю расписание", "running") as never,
      ],
    });
    const { turns } = build({ messages, jobs: [live] });
    expect(kinds(turns)).toEqual(["user", "block", "block", "thinking"]);
    const last = turns.at(-1);
    expect(last).toMatchObject({ kind: "thinking", jobId: task, running: true });
    expect(last?.kind === "thinking" && last.steps.map((step) => step.label)).toEqual([
      "Ищу личный кабинет",
      "Читаю расписание",
    ]);
  });

  it("новое сообщение человека не прячет живое «Думает»: оно по-прежнему последнее", () => {
    const messages = [agent(2, "connect-1", NOW - 60_000, say(21, NOW - 60_000, "Захожу."))];
    const live = job("connect-1", "connect", {
      blocks: [thought(20, NOW - 80_000, "Читаю", "running") as never],
    });
    const { turns } = build({ messages, jobs: [live], pending: [pending({ status: "sending" })] });
    expect(kinds(turns)).toEqual(["block", "user", "thinking"]);
  });

  it("закончилось: свёрнутый «Думал» стоит прямо над итоговым ответом этого задания", () => {
    const task = "connect-2";
    const messages = [
      message({
        id: ULID(1),
        role: "user",
        blocks: [textBlock(ULID(10), "Подключи")],
        createdAt: iso(NOW - 120_000),
      }),
      agent(2, task, NOW - 110_000, thought(20, NOW - 110_000, "Ищу вход")),
      agent(3, task, NOW - 100_000, say(21, NOW - 100_000, "Нашла вход.")),
      agent(4, task, NOW - 60_000, thought(22, NOW - 60_000, "Читаю расписание")),
      message({
        id: ULID(5),
        role: "user",
        blocks: [textBlock(ULID(11), "Да, всё верно")],
        createdAt: iso(NOW - 50_000),
      }),
      agent(6, task, NOW - 10_000, say(23, NOW - 10_000, "Готово! Расписание подключено.")),
    ];
    const { turns } = build({ messages });
    expect(kinds(turns)).toEqual(["user", "block", "user", "thinking", "block"]);
    expect(turns[3]).toMatchObject({ kind: "thinking", jobId: task, running: false });
    expect(turns[4]).toMatchObject({ kind: "block", jobId: task });
    expect(turns[4]?.kind === "block" && turns[4].block.type === "text" && turns[4].block.markdown).toBe(
      "Готово! Расписание подключено.",
    );
  });

  it("закончившееся задание в потоке: «Думал» над последним текстом, замирает на его времени", () => {
    const done = job("chat-9", "chat", {
      state: "done",
      blocks: [thought(20, NOW - 9_000, "Читаю") as never, say(21, NOW - 2_000, "Вот ответ") as never],
    });
    const { turns } = build({ messages: history, jobs: [done], nowMs: NOW + 60_000 });
    expect(kinds(turns).slice(-2)).toEqual(["thinking", "block"]);
    expect(turns.at(-2)).toMatchObject({ running: false, finishedAt: NOW - 2_000 });
  });

  it("сбой задания: «Думал» над сообщением о сбое, живое соседнее задание остаётся внизу", () => {
    const failed = job("c1", "connect", {
      state: "failed",
      problem: { code: "source_unavailable", detail: "", extra: {} },
      blocks: [thought(20, NOW - 9_000, "Захожу", "failed") as never],
    });
    const working = job("chat-8", "chat", { blocks: [thought(21, NOW - 3_000, "Ищу", "running") as never] });
    const { turns } = build({ messages: history, jobs: [failed, working] });
    expect(kinds(turns)).toEqual(["user", "block", "thinking", "failure", "thinking"]);
    expect(turns.at(-1)).toMatchObject({ jobId: "chat-8", running: true });
    expect(turns.at(-3)).toMatchObject({ jobId: "c1", running: false });
  });

  it("у закончившегося задания нет ни одного своего блока в ленте — «Думал» остаётся на своём месте", () => {
    const messages = [
      ...history,
      agent(8, "connect-3", NOW - 500_000, thought(20, NOW - 500_000, "Смотрю сайт")),
      message({
        id: ULID(9),
        role: "user",
        blocks: [textBlock(ULID(12), "Ещё вопрос")],
        createdAt: iso(NOW - 400_000),
      }),
    ];
    const { turns } = build({ messages });
    expect(kinds(turns)).toEqual(["user", "block", "thinking", "user"]);
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

  it("такой вопрос показывает «Жду ответ агента» и блокирует поле ввода", () => {
    const { turns } = build({ messages: [ask] });
    expect(turns.at(-1)).toMatchObject({ kind: "thinking", jobId: null, running: true });
  });
});

const jobOut = (id: string, kind: string, state: JobOut["state"], extra: Partial<JobOut> = {}): JobOut => ({
  id,
  kind,
  connectionId: null,
  state,
  stage: null,
  createdAt: iso(NOW - 10_000),
  startedAt: iso(NOW - 9_000),
  finishedAt: null,
  ...extra,
});
const runtime = (extra: Partial<ThreadRuntime> = {}): ThreadRuntime => ({
  activeJob: null,
  tasks: [],
  pending: [],
  receipts: [],
  ...extra,
});

describe("состояние треда из runtime", () => {
  it("потоки нужны ходу агента и незавершённым задачам; завершённая задача потока не просит", () => {
    const rt = runtime({
      activeJob: jobOut("chat-1", "chat", "running"),
      tasks: [jobOut("c1", "connect", "waiting_user"), jobOut("c0", "sync", "done")],
    });
    expect(runtimeJobs(rt).map((item) => item.id)).toEqual(["chat-1", "c1"]);
    expect(runtimeJobs(undefined)).toEqual([]);
  });

  it("mergeJob: серверное состояние — основа, событий потока ещё нет — состояние сервера", () => {
    const saved = jobOut("c1", "connect", "waiting_user", { stage: "Жду логин" });
    const merged = mergeJob(saved, undefined);
    expect(merged).toMatchObject({ jobId: "c1", state: "waiting_user", stage: "Жду логин" });
    const live = snapshot("c1", { state: "running", stage: "Читаю", lastEventId: "5" });
    expect(mergeJob(saved, live)).toMatchObject({ state: "running", stage: "Читаю" });
    // Завершённое сервером задание поток не оживляет.
    expect(mergeJob(jobOut("c1", "connect", "done"), live).state).toBe("done");
  });

  it("поле ввода: загрузка, вопрос в блоке, ход агента, диалог с задачей, свободно", () => {
    const base = { threadId: "t1", runtimeLoading: false, runtimeFailed: false, jobs: [], awaiting: false };
    expect(composerState({ ...base, threadId: null, runtime: undefined })).toMatchObject({ disabled: false });
    expect(composerState({ ...base, runtime: undefined, runtimeLoading: true })).toMatchObject({
      disabled: true,
    });

    // Задача ждёт защищённого ответа: агенту всё равно можно написать, сообщение уходит обычным ходом.
    const task = job("c1", "connect");
    const secret = runtime({ pending: [{ jobId: "c1", blockId: "b", channel: "native", secret: true }] });
    const waiting = composerState({ ...base, runtime: secret, jobs: [task] });
    expect(waiting).toMatchObject({
      disabled: false,
      waitingForHuman: true,
      placeholder: "Пароль вводите в защищённое поле выше",
    });
    // …и без адресата: пароль не должен уходить в «диалог» задачи.
    expect(waiting.targetJobId).toBeUndefined();
    const manual = runtime({ pending: [{ jobId: "c1", blockId: "b", channel: "manual", secret: false }] });
    expect(composerState({ ...base, runtime: manual, jobs: [task] })).toMatchObject({
      disabled: false,
      placeholder: "Вход завершите в браузере выше или напишите агенту",
    });
    // Вопрос самого хода агента держит ход: писать, пока он идёт, нельзя.
    const approval = runtime({
      pending: [{ jobId: "chat-1", blockId: "b", channel: "approval", secret: false }],
    });
    expect(composerState({ ...base, runtime: approval, jobs: [job("chat-1", "chat")] })).toMatchObject({
      disabled: true,
      waitingForHuman: true,
      placeholder: "Ответьте на вопрос выше",
    });
    // Идёт ход агента, а у задачи защищённый вопрос: пока агент отвечает, поле занято.
    expect(composerState({ ...base, runtime: secret, jobs: [task, job("chat-2", "chat")] })).toMatchObject({
      disabled: true,
      replying: true,
    });

    const chat = job("chat-1", "chat");
    expect(composerState({ ...base, runtime: runtime(), jobs: [chat] })).toMatchObject({
      disabled: true,
      replying: true,
    });
    expect(composerState({ ...base, runtime: runtime(), awaiting: true })).toMatchObject({ replying: true });

    // Задача идёт в фоне: поле свободно, сообщение уходит без 409.
    expect(composerState({ ...base, runtime: runtime(), jobs: [task] })).toMatchObject({
      disabled: false,
      replying: false,
    });
    const dialogue = runtime({
      pending: [{ jobId: "c1", blockId: "b", channel: "dialogue", secret: false }],
    });
    expect(composerState({ ...base, runtime: dialogue, jobs: [task] })).toMatchObject({
      disabled: false,
      waitingForHuman: true,
      targetJobId: "c1",
    });
    expect(composerState({ ...base, runtime: runtime() })).toMatchObject({
      disabled: false,
      replying: false,
    });
  });

  it("вопросы с вводом у поля: только поток уже прислал блок, диалоговые вопросы — в поле ввода", () => {
    const question = {
      type: "input",
      id: ULID(70),
      at: iso(NOW),
      question: "Пароль",
      inputKind: "password",
      required: true,
      secret: true,
    } as never;
    const live = job("c1", "connect", { state: "waiting_user", blocks: [question] });
    const rt = runtime({
      pending: [
        { jobId: "c1", blockId: ULID(70), channel: "native", secret: true },
        { jobId: "c1", blockId: ULID(71), channel: "dialogue", secret: false },
        { jobId: "gone", blockId: ULID(72), channel: "native", secret: false },
      ],
    });
    const editors = pendingEditors(rt, [live]);
    expect(editors.map((item) => item.key)).toEqual([`block:c1:${ULID(70)}`]);
    // Блока ещё нет в потоке: рисовать нечего.
    expect(pendingEditors(rt, [job("c1", "connect", { state: "waiting_user" })])).toEqual([]);
    // Завершённое задание вопросов не держит.
    expect(pendingEditors(rt, [job("c1", "connect", { state: "done", blocks: [question] })])).toEqual([]);
  });

  it("задание остаётся в ленте, пока его блоков нет в истории", () => {
    const saved = message({
      id: ULID(8),
      role: "agent",
      jobId: "chat-9",
      blocks: [textBlock(ULID(80), "Ответ")],
    });
    const snap = snapshot("chat-9", {
      state: "done",
      blocks: [
        { type: "text", id: ULID(80), at: iso(NOW), markdown: "Ответ" },
        {
          type: "handoff",
          id: ULID(81),
          at: iso(NOW),
          title: "Заявка",
          url: "https://x.test",
          text: "Текст",
        },
        { type: "thinking", id: ULID(82), at: iso(NOW), stage: "Шаг", state: "ok" },
      ],
    });
    expect(unsavedBlocks(snap, [saved])).toBe(1);
    expect(unsavedBlocks(snapshot("chat-9", { blocks: [] }), [saved])).toBe(0);
  });

  it("userText склеивает только текстовые блоки", () => {
    expect(
      userText(
        message({
          id: ULID(9),
          role: "user",
          blocks: [
            textBlock(ULID(90), "Раз"),
            { type: "attachment", id: ULID(91) },
            textBlock(ULID(92), "Два"),
          ],
        }),
      ),
    ).toBe("Раз\n\nДва");
  });
});

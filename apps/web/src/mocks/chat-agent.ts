/**
 * «Агент» мок-режима: один агент на тред, который отвечает и запускает задачи. Отвечает по готовым данным
 * (`data-seed.ts`), по ключевым словам вопроса:
 *   пара, расписание → таблица пар на сегодня;  срок, дедлайн, сдать → ближайшие открытые работы;
 *   график, диаграмма → график `chart`;  сложно, план, сравни → план ответа, затем таблица;
 *   документ, правила → документы пользователя;  справка, заявление → вопрос, затем запрос на запись;
 *   подключи, добавь сайт → фоновая задача подключения в этом же треде;  сбой → ошибка квоты;
 *   всё остальное → честное «Не знаю: …».
 * Вложения сообщения агент называет по виду: аудио — «расшифровки пока нет», как и должно быть (FR-046).
 * Ответ приходит так же, как у сервера: этапы блоками `thinking`, затем блок `text` с источниками.
 */
import type { PlanBlock, TaskBlock, TaskKind } from "@/api/v3";
import { addDays, startOfDay } from "@/features/data/dates";
import type { KnownBlock } from "@/features/jobs/blocks";
import {
  type ChatState,
  DEFAULT_TITLE,
  type Message,
  type MockAttachment,
  type MockThread,
  type Source,
  textBlock,
} from "./chat-state";
import { runConnect } from "./connect-script";
import { dueOf, MOCK_ZONE, seedDeadlines, seedDocuments, seedEvents } from "./data-seed";
import { type MockDb, ulid } from "./db";
import { JobCancelledError, JobSim } from "./job-sim";

export interface AgentCtx {
  db: MockDb;
  state: () => ChatState;
  save: () => void;
  iso: (ms?: number) => string;
  jobPace: number;
  find: (threadId: string) => MockThread | undefined;
}

const KEYWORDS = {
  schedule: /пар[аыуе]|расписан|занят|лекци|семинар/i,
  deadlines: /срок|дедлайн|сда[тв]|задани|просроч|горит/i,
  documents: /документ|правил|положени|курсов|пересдач/i,
  handoff: /вручную|сам[ао]?\s+отправ|через сайт/i,
  request: /справк|заявлен|запиш|консультац/i,
  failure: /сбой/i,
  connect: /подключ|добавь (сайт|источник|вуз)|smart lms/i,
  chart: /график|диаграмм/i,
  plan: /сложн|составь план|сравн/i,
} as const;

type Intent = keyof typeof KEYWORDS | "unknown";

export function intentOf(question: string): Intent {
  for (const key of [
    "failure",
    "handoff",
    "connect",
    "request",
    "chart",
    "plan",
    "schedule",
    "deadlines",
    "documents",
  ] as const) {
    if (KEYWORDS[key].test(question)) return key;
  }
  return "unknown";
}

const clock = (ms: number) =>
  new Intl.DateTimeFormat("ru-RU", {
    timeZone: MOCK_ZONE,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(ms);
const dayMonth = (ms: number) =>
  new Intl.DateTimeFormat("ru-RU", { timeZone: MOCK_ZONE, day: "numeric", month: "long" }).format(ms);

/** Какие блоки сервер сохраняет в историю треда: у хода агента это ответ, план и карточка задачи. */
const TURN_KEPT = new Set(["text", "plan", "task", "attachment"]);

/**
 * Проекция событий задания в сообщения треда, как это делает сервер: блок с тем же `id` обновляется на месте
 * (план), у задач сохраняется всё, у хода агента — только ответ, план и карточка задачи.
 */
export function observe(ctx: AgentCtx, sim: JobSim, threadId: string): void {
  const project = (event: (typeof sim.events)[number]) => {
    const messages = ctx.state().messages[threadId] ?? [];
    if (event.event === "block") {
      const raw = event.data as KnownBlock;
      if (sim.kind === "chat" && !TURN_KEPT.has(raw.type)) return;
      const block =
        raw.type === "login"
          ? { type: "text" as const, id: raw.id, at: raw.at, markdown: "Вход через браузер" }
          : raw;
      const existing = messages.find(
        (message) => message.jobId === sim.jobId && message.blocks.some((item) => item.id === block.id),
      );
      if (existing) {
        existing.blocks = existing.blocks.map((item) => (item.id === block.id ? (block as never) : item));
      } else {
        messages.push({
          id: ulid(),
          role: "agent",
          jobId: sim.jobId,
          blocks: [block as never],
          createdAt: block.at,
        });
      }
    } else if (event.event === "block_update") {
      const update = event.data as { id: string; patch: Record<string, unknown> };
      for (const message of messages)
        if (message.jobId === sim.jobId)
          message.blocks = message.blocks.map((block) =>
            block.id === update.id ? { ...block, ...update.patch } : block,
          );
    } else return;
    ctx.state().messages[threadId] = messages;
    const thread = ctx.find(threadId);
    if (thread) {
      const at = event.event === "block" ? (event.data as KnownBlock).at : ctx.iso();
      if (at > thread.lastMessageAt) thread.lastMessageAt = at;
    }
    ctx.save();
  };
  for (const event of sim.events) project(event);
  sim.onEvent = project;
}

export interface TaskInput {
  kind: TaskKind;
  url: string | null;
  university: string | null;
  spaceId?: string | null;
  title?: string;
}

/** Фоновая задача подключения в треде: находит вуз, заводит источник и проходит вход вместе с человеком. */
export function startTask(ctx: AgentCtx, threadId: string, input: TaskInput): JobSim {
  const { db } = ctx;
  const sim = new JobSim(ulid(), db.now, ctx.jobPace, null, input.kind);
  db.sims.set(sim.jobId, sim);
  const jobs = ctx.state().jobs[threadId] ?? [];
  ctx.state().jobs[threadId] = [...jobs, sim.jobId];
  observe(ctx, sim, threadId);
  void runTask(ctx, sim, threadId, input);
  return sim;
}

async function runTask(ctx: AgentCtx, sim: JobSim, threadId: string, input: TaskInput): Promise<void> {
  const { db } = ctx;
  try {
    const url =
      input.url ??
      (await sim.ask(
        {
          type: "input",
          id: ulid(),
          at: ctx.iso(),
          question: "Пришлите ссылку на источник, который хотите подключить.",
          inputKind: "url",
          secret: false,
          required: true,
          maxLength: 1000,
        },
        "Жду ссылку на источник",
      ));
    const university = input.university
      ? db.universities.find((item) => item.id === input.university)
      : undefined;
    let space =
      db.spaces.find((item) => item.id === input.spaceId) ??
      db.spaces.find((item) => item.universityId === university?.id);
    if (!space) {
      space = {
        id: ulid(),
        universityId: university?.id ?? null,
        title: university?.title ?? "Мой вуз",
        shortTitle: university?.shortTitle ?? "Мой вуз",
        aliases: university?.aliases ?? [],
        accentHue: university?.accentHue ?? 325,
        timeZone: "Europe/Moscow",
        createdAt: ctx.iso(),
      };
      db.spaces.push(space);
    }
    const connection = {
      id: ulid(),
      spaceId: space.id,
      title: input.title ?? "Личный кабинет",
      origin: typeof url === "string" ? url : "https://lk.fakeuni.test",
      kind: "portal" as const,
      adapter: null,
      status: "learning" as const,
      hasPassword: false,
      lastSyncAt: null,
      nextSyncAt: null,
      errorCode: null,
      errorDetail: null,
      // §3.5: у источника есть тред, в котором его подключали.
      threadId,
    };
    db.connections.push(connection);
    sim.connectionId = connection.id;
    const thread = ctx.find(threadId);
    if (thread) thread.spaceId = space.id;
    await runConnect(sim, db, connection.id, db.jobVariant);
  } catch (error) {
    if (!(error instanceof JobCancelledError)) throw error;
  } finally {
    ctx.save();
  }
}

function tableOfToday(db: MockDb, now: number) {
  const dayStart = startOfDay(now, MOCK_ZONE);
  const today = seedEvents(db)
    .filter((event) => {
      const start = Date.parse(event.startsAt);
      return start >= dayStart && start < addDays(dayStart, 1, MOCK_ZONE);
    })
    .sort((a, b) => Date.parse(a.startsAt) - Date.parse(b.startsAt));
  const next = today.find((event) => Date.parse(event.startsAt) > now);
  return { today, next, last: today.at(-1) };
}

const attachmentReply = (files: readonly MockAttachment[]): string =>
  files
    .map((file) => {
      switch (file.kind) {
        case "audio":
          return `Аудиофайл «${file.name}» сохранён, но расшифровки пока нет: напишите, что нужно, текстом.`;
        case "image":
          return `Изображение «${file.name}» получено. Распознавание картинок пока не подключено, поэтому описать его я не могу.`;
        case "document":
          return `Документ «${file.name}» получен. Поиск по нему уже работает: спросите, что в нём написано.`;
        default:
          return `Файл «${file.name}» сохранён, но прочитать его я не могу.`;
      }
    })
    .join("\n\n");

export interface ReplyInput {
  question: string;
  files: readonly MockAttachment[];
}

/** Ход агента: этапы, затем ответ. Блоки идут в поток задания, а в историю треда попадают по правилам сервера. */
export async function runReply(
  ctx: AgentCtx,
  sim: JobSim,
  threadId: string,
  input: ReplyInput,
): Promise<void> {
  const { db, state } = ctx;
  const { question, files } = input;
  const at = () => ctx.iso();
  const step = async (stage: string, ms = 600, detail?: string) => {
    const id = ulid();
    sim.status(stage);
    sim.block({ type: "thinking", id, at: at(), stage, state: "running", detail });
    await sim.pause(ms);
    sim.update(id, { state: "ok" });
  };
  let finalText = "";
  let sources: Source[] = [];
  let quickReplies: string[] | undefined;
  let usage = { data: 0, documents: 0 };
  try {
    sim.status("Читаю вопрос");
    await sim.pause(300);
    const intent = question.trim() ? intentOf(question) : "unknown";
    const connection = db.connections.find((item) => item.kind === "portal") ?? db.connections[0];
    const fetchedAt = ctx.iso(db.now() - 12 * 60_000);

    if (files.length > 0 && !question.trim()) {
      await step("Смотрю вложения", 400);
      finalText = attachmentReply(files);
      quickReplies = files.some((file) => file.kind === "audio") ? ["Что у меня сегодня?"] : undefined;
    } else if (intent === "failure") {
      await step("Ищу в подключённых источниках", 400);
      sim.emit("error", {
        type: "https://botai.mnema.app/errors/quota_exceeded",
        title: "quota_exceeded",
        status: 429,
        code: "quota_exceeded",
        detail: "",
      });
      return;
    } else if (intent === "connect") {
      await step("Проверяю, что уже подключено", 400);
      const url = /https?:\/\/\S+/.exec(question)?.[0] ?? null;
      const known = url ? db.connections.find((item) => item.origin === url) : undefined;
      if (known) {
        finalText = `Этот источник уже подключён — «${known.title}». Перепроверить данные или войти заново?`;
        quickReplies = ["Перепроверить", "Войти заново"];
      } else {
        const hse = /вышк|вшэ/i.test(question);
        const cu = /цу|центральн/i.test(question);
        const task = startTask(ctx, threadId, {
          kind: "connect",
          url,
          university: cu ? "u-cu" : hse ? "u-hse" : null,
          title: cu || hse ? "Личный кабинет" : undefined,
        });
        const card: TaskBlock = {
          type: "task",
          id: ulid(),
          at: at(),
          jobId: task.jobId,
          kind: "connect",
          title: cu ? "Подключение ЦУ" : hse ? "Подключение ВШЭ" : "Подключение источника",
          state: "running",
          threadId,
        };
        sim.block(card);
        finalText = url
          ? "Запустил подключение. Понадобится ваш вход — спрошу прямо здесь."
          : "Запустил подключение. Пришлите ссылку на источник — я продолжу.";
      }
    } else if (intent === "plan") {
      const steps = [
        { id: "s1", title: "Собрать пары на сегодня" },
        { id: "s2", title: "Собрать открытые работы" },
        { id: "s3", title: "Сравнить нагрузку по дням" },
      ];
      const planId = ulid();
      // Один и тот же `id`: клиент заменяет блок на месте, план обновляется, а не дублируется.
      const showPlan = (done: number) =>
        sim.block({
          type: "plan",
          id: planId,
          at: at(),
          title: "Как отвечу",
          steps: steps.map((item, index) => ({
            ...item,
            state: index < done ? "done" : index === done ? "active" : "pending",
          })) as PlanBlock["steps"],
        } satisfies PlanBlock);
      showPlan(0);
      await step("Читаю расписание", 400);
      showPlan(1);
      await step("Читаю открытые работы", 400);
      showPlan(2);
      await step("Сравниваю нагрузку", 400);
      showPlan(3);
      finalText =
        "Нагрузка на неделе распределена так:\n\n| День | Пар | Сроков |\n|:---|---:|---:|\n| Понедельник | 3 | **1** |\n| Вторник | 2 | 0 |\n| Среда | 4 | **2** |\n\n*Больше всего сроков в среду* — стоит начать с неё. Подробности — [в личном кабинете](https://lk.fakeuni.test/timetable).";
      quickReplies = ["Что сдавать на неделе?", "Покажи график"];
      usage = { data: 6_100, documents: 0 };
    } else if (intent === "chart") {
      await step("Собираю данные для графика", 500);
      finalText =
        "Пар по дням недели:\n\n```chart\n" +
        JSON.stringify({
          type: "bar",
          title: "Пар по дням",
          labels: ["Пн", "Вт", "Ср", "Чт", "Пт"],
          series: [{ name: "Пар", values: [3, 2, 4, 3, 1] }],
        }) +
        "\n```\n\nВ среду самая плотная нагрузка.";
      usage = { data: 2_400, documents: 0 };
    } else if (intent === "schedule") {
      await step("Ищу расписание в подключённых источниках");
      await step("Читаю расписание на сегодня", 700);
      const { today, next, last } = tableOfToday(db, db.now());
      if (!connection || today.length === 0) {
        finalText =
          "Не знаю: в подключённых источниках нет занятий на сегодня. Подключите вуз или проверьте, что расписание обновилось.";
      } else {
        const rows = today
          .map(
            (event) =>
              `| ${clock(Date.parse(event.startsAt))} | ${event === next ? `**${event.title}**` : event.title} | ${event.location ?? "—"} |`,
          )
          .join("\n");
        const table = `| Время | Предмет | Аудитория |\n|:---|:---|:---|\n${rows}`;
        const link = `*Расписание обновлено 12 минут назад.* Подробнее — [в кабинете](${(next ?? last)?.url ?? "https://lk.fakeuni.test/timetable"}).`;
        finalText = next
          ? `Следующая пара — **${next.title}** в ${clock(Date.parse(next.startsAt))}${next.location ? `, ${next.location}` : ""}.\n\n${table}\n\n${link}`
          : `На сегодня пары закончились. Последняя — **${last?.title}** в ${clock(Date.parse(last?.startsAt ?? ""))}.\n\n${table}\n\n${link}`;
        // Три страницы одного подключения: под ответом это одна запись «Расписание — личный кабинет».
        sources = [
          { title: "Расписание — личный кабинет", url: `botai://connections/${connection.id}`, fetchedAt },
          { title: "Расписание на сегодня", url: `${connection.origin}/timetable`, fetchedAt },
          { title: "Расписание на неделю", url: `${connection.origin}/timetable/week`, fetchedAt },
        ];
        quickReplies = ["Что сдавать на неделе?", "Покажи график"];
        usage = { data: 5_400, documents: 0 };
      }
    } else if (intent === "deadlines") {
      await step("Ищу сроки в подключённых источниках");
      await step("Читаю открытые работы", 700);
      const open = seedDeadlines(db)
        .filter((row) => row.state === "open" || row.state === "missed")
        .sort((a, b) => (dueOf(a) ?? Number.POSITIVE_INFINITY) - (dueOf(b) ?? Number.POSITIVE_INFINITY))
        .slice(0, 3);
      if (open.length === 0 || !connection) {
        finalText =
          "Не знаю: в подключённых источниках нет открытых работ. Если они должны быть, проверьте подключение.";
      } else {
        finalText =
          "Ближайшие работы:\n\n" +
          open
            .map((row) => {
              const due = dueOf(row);
              return `- **${row.title}** — ${due === null ? "срок не указан" : `до ${dayMonth(due)}, ${clock(due)}`}`;
            })
            .join("\n");
        sources = [
          { title: "Задания — личный кабинет", url: `botai://connections/${connection.id}`, fetchedAt },
        ];
        usage = { data: 3_900, documents: 0 };
      }
    } else if (intent === "documents") {
      await step("Ищу в документах");
      await step("Читаю найденные документы", 700);
      const doc = seedDocuments(db).find((row) => /правил/i.test(row.title)) ?? seedDocuments(db)[0];
      if (!doc) {
        finalText =
          "Не знаю: у меня нет документов по этому вопросу. Загрузите документ или подключите страницу с правилами.";
      } else {
        finalText = `Об этом написано в документе «${doc.title}». Курсовую работу сдают через личный кабинет до начала сессии.`;
        sources = [
          { title: doc.title, url: `botai://documents/${doc.id}`, fetchedAt: doc.fetchedAt },
          ...(doc.url ? [{ title: "Страница на сайте вуза", url: doc.url, fetchedAt: doc.fetchedAt }] : []),
        ];
        usage = { data: 0, documents: 7_800 };
      }
    } else if (intent === "handoff") {
      await step("Проверяю форму на сайте вуза");
      sim.block({
        type: "handoff",
        id: ulid(),
        at: at(),
        title: "Заявка на справку",
        url: "https://lk.fakeuni.test/requests/new",
        text: "Прошу выдать справку об обучении для предоставления по месту требования.",
      });
      finalText =
        "Форма на сайте вуза не позволяет отправить заявку отсюда. Текст готов: скопируйте его и отправьте сами по ссылке выше.";
      usage = { data: 600, documents: 0 };
    } else if (intent === "request") {
      await step("Проверяю, можно ли подать заявку из BotAI");
      const kind = await sim.ask(
        {
          type: "choice",
          id: ulid(),
          at: at(),
          question: "Какая справка нужна?",
          mode: "single",
          required: true,
          allowOther: false,
          options: [
            { value: "study", label: "Об обучении" },
            { value: "scholarship", label: "О стипендии" },
          ],
        },
        "Жду ответа про справку",
      );
      const label = Array.isArray(kind) ? kind[0] : kind;
      const proposal = ulid();
      const confirmed = await sim.ask(
        {
          type: "approval",
          id: ulid(),
          at: at(),
          proposal: {
            id: proposal,
            connectionId: connection?.id ?? ulid(),
            intent: "Подать заявку на справку",
            reversible: false,
            expiresAt: ctx.iso(db.now() + 600_000),
            request: {
              method: "POST",
              url: "https://lk.fakeuni.test/requests/certificates",
              bodyKind: "form",
              fields: [
                {
                  name: "type",
                  label: "Тип справки",
                  value: label === "scholarship" ? "О стипендии" : "Об обучении",
                  editable: false,
                },
                { name: "copies", label: "Количество копий", value: "1", editable: true },
              ],
            },
          },
        },
        "Жду вашего подтверждения",
      );
      finalText =
        confirmed === true
          ? "Заявка отправлена. Статус появится в личном кабинете вуза."
          : "Ничего не отправлено. Если передумаете, напишите ещё раз.";
      usage = { data: 600, documents: 0 };
    } else {
      await step("Ищу в подключённых источниках", 500);
      finalText =
        "Не знаю: в подключённых источниках нет данных по этому вопросу. Уточните, о каком предмете или документе речь, или подключите источник.";
    }

    if (files.length > 0 && question.trim()) finalText = `${attachmentReply(files)}\n\n${finalText}`;
    sim.block(textBlock(finalText, at(), sources, quickReplies));
    const chat = state();
    chat.last[threadId] = { ...usage, sources };
    const thread = ctx.find(threadId);
    if (thread && thread.title === DEFAULT_TITLE) {
      const line = (question || files[0]?.name || "").replace(/\s+/g, " ").trim();
      thread.title = line.length > 60 ? `${line.slice(0, 57)}…` : line || DEFAULT_TITLE;
    }
    sim.status("Готово");
    sim.emit("done", {
      result: {
        stopped: "answer",
        steps: 2,
        sources: sources.map((source) => ({
          title: source.title,
          kind: "data",
          fetchedAt: source.fetchedAt,
        })),
      },
    });
  } catch (error) {
    if (!(error instanceof JobCancelledError)) throw error;
  } finally {
    delete state().busy[threadId];
    ctx.save();
  }
}

export type { Message };

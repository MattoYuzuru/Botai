/**
 * Обработчики MSW для чата: треды, сообщения, шкала контекста и задание ответа (поток `GET /jobs/{id}/events` отдают
 * общие обработчики заданий). Форма ответов — по OpenAPI бэкенда (`botai/agent/router.py`); эндпоинтов, которых там
 * нет, здесь нет.
 *
 * «Агент» мока отвечает по готовым данным (`data-seed.ts`), по ключевым словам вопроса:
 *   пара, расписание → следующая пара из «Сегодня»;  срок, дедлайн, сдать → ближайшие открытые работы;
 *   документ, правила → документы пользователя;  справка, заявление → вопрос, затем запрос на запись (ApprovalCard);
 *   сбой → задание завершается ошибкой квоты;  всё остальное → честное «Не знаю: …».
 * Ответ приходит так же, как у сервера: этапы блоками `thinking`, затем один блок `text` с источниками.
 */
import { delay, HttpResponse, http } from "msw";
import type { AgentUiBlock } from "@/api/agent-ui.gen";
import type { components } from "@/api/schema.gen";
import type { ConnectContext, ThreadRuntime } from "@/features/chat/connection-api";
import { addDays, startOfDay } from "@/features/data/dates";
import { runConnect } from "./connect-script";
import { dueOf, MOCK_ZONE, seedDeadlines, seedDocuments, seedEvents } from "./data-seed";
import { findUser, type MockDb, ulid } from "./db";
import { JobCancelledError, JobSim } from "./job-sim";

type S = components["schemas"];
type Thread = S["ThreadOut"];
type Message = S["MessageOut"];
type Source = { title: string; url: string; fetchedAt: string; stale?: boolean };

export interface ChatHandlerOptions {
  latency?: number;
  persist?: boolean;
  /** Множитель пауз ответа агента: 0 — мгновенно (тесты). */
  jobPace?: number;
}

const DEFAULT_TITLE = "Новый чат";
const CONTEXT_LIMIT = 1_000_000;
const STORAGE_KEY = "botai.mock.chat";

interface ChatState {
  threads: Thread[];
  messages: Record<string, Message[]>;
  /** Тред → задание, которое сейчас отвечает (как `Job.state in (queued, running, waiting_user)`). */
  busy: Record<string, string>;
  /** Что записало последнее завершённое задание чата: для шкалы контекста. */
  last: Record<string, { data: number; documents: number; sources: Source[] }>;
  connections: Record<string, { connectionId: string | null; jobs: string[] }>;
  requests: Record<string, string>;
  accepted: Record<string, S["MessageAccepted"]>;
}

function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const states = new WeakMap<MockDb, ChatState>();

const textBlock = (markdown: string, at: string, sources?: Source[]): AgentUiBlock => ({
  type: "text",
  id: ulid(),
  at,
  markdown,
  ...(sources && sources.length > 0 ? { sources } : {}),
});

/** Тред с готовой перепиской: показывает историю и внутренние ссылки источников. */
function seedState(db: MockDb): ChatState {
  const state: ChatState = {
    threads: [],
    messages: {},
    busy: {},
    last: {},
    connections: {},
    requests: {},
    accepted: {},
  };
  const connection = db.connections.find((item) => item.kind === "portal") ?? db.connections[0];
  if (!connection) return state;
  const at = (minutesAgo: number) => new Date(db.now() - minutesAgo * 60_000).toISOString();
  const thread: Thread = {
    id: ulid(),
    spaceId: null,
    title: "Что сегодня по расписанию?",
    createdAt: at(180),
    updatedAt: at(176),
  };
  const fetchedAt = at(12);
  const sources: Source[] = [
    { title: "Расписание — личный кабинет", url: `botai://connections/${connection.id}`, fetchedAt },
    { title: "Расписание на сайте вуза", url: "https://lk.fakeuni.test/timetable", fetchedAt },
  ];
  state.threads.push(thread);
  state.messages[thread.id] = [
    {
      id: ulid(),
      role: "user",
      blocks: [textBlock("Что сегодня по расписанию?", at(180)) as never],
      jobId: null,
      createdAt: at(180),
    },
    {
      id: ulid(),
      role: "agent",
      blocks: [
        textBlock(
          "Сегодня три пары. Первая — **Алгоритмы и структуры данных** в 9:30 [1], затем «Базы данных» [2].",
          at(176),
          sources,
        ) as never,
      ],
      jobId: ulid(),
      createdAt: at(176),
    },
  ];
  state.last[thread.id] = { data: 4_200, documents: 0, sources };
  return state;
}

function loadState(db: MockDb, persist: boolean): ChatState {
  let state = states.get(db);
  if (state) return state;
  if (persist) {
    try {
      const raw = window.sessionStorage.getItem(STORAGE_KEY);
      if (raw) state = { ...(JSON.parse(raw) as ChatState), busy: {} };
    } catch {
      /* хранилище недоступно */
    }
  }
  state ??= seedState(db);
  state.connections ??= {};
  state.requests ??= {};
  state.accepted ??= {};
  states.set(db, state);
  return state;
}

const estimate = (text: string) => Math.ceil(text.length / 3);

const KEYWORDS = {
  schedule: /пар[аыуе]|расписан|занят|лекци|семинар/i,
  deadlines: /срок|дедлайн|сда[тв]|задани|просроч|горит/i,
  documents: /документ|правил|положени|курсов|пересдач/i,
  handoff: /вручную|сам[ао]?\s+отправ|через сайт/i,
  request: /справк|заявлен|запиш|консультац/i,
  failure: /сбой/i,
} as const;

type Intent = keyof typeof KEYWORDS | "unknown";

export function intentOf(question: string): Intent {
  for (const key of ["failure", "handoff", "request", "schedule", "deadlines", "documents"] as const) {
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

export function createChatHandlers(db: MockDb, options: ChatHandlerOptions = {}) {
  const { latency = 0, persist = false, jobPace = 0 } = options;
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const user = () => findUser(db, db.sessionUserId);
  const state = () => loadState(db, persist);
  const save = () => {
    if (!persist) return;
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state()));
    } catch {
      /* хранилище недоступно */
    }
  };
  const iso = (ms = db.now()) => new Date(ms).toISOString();
  const find = (threadId: string) => state().threads.find((item) => item.id === threadId);

  const observe = (sim: JobSim, threadId: string) => {
    const project = (event: (typeof sim.events)[number]) => {
      const messages = state().messages[threadId] ?? [];
      if (event.event === "block") {
        const raw = event.data as AgentUiBlock;
        const block =
          raw.type === "login"
            ? { type: "text", id: raw.id, at: raw.at, markdown: "Вход через браузер" }
            : raw;
        if (
          !messages.some(
            (message) => message.jobId === sim.jobId && message.blocks.some((item) => item.id === block.id),
          )
        ) {
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
      }
      state().messages[threadId] = messages;
      const thread = find(threadId);
      if (thread) thread.updatedAt = iso();
      save();
    };
    for (const event of sim.events) project(event);
    sim.onEvent = project;
  };

  const runtimeFor = (threadId: string): ThreadRuntime => {
    const linked = state().connections[threadId];
    const connected = linked?.connectionId
      ? (db.connections.find((item) => item.id === linked.connectionId) ?? null)
      : null;
    const sims = linked
      ? linked.jobs.map((id) => db.sims.get(id)).filter((item): item is JobSim => Boolean(item))
      : [];
    const ordinary = db.sims.get(state().busy[threadId] ?? "");
    const sim =
      sims.findLast((item) => !item.terminal) ?? (ordinary && !ordinary.terminal ? ordinary : sims.at(-1));
    const status = sim?.events.findLast((event) => event.event === "status")?.data as
      | { stage?: string }
      | undefined;
    const done = sim?.events.findLast((event) => event.event === "done")?.data as
      | { result?: Record<string, unknown> }
      | undefined;
    const error = sim?.events.findLast((event) => event.event === "error")?.data as
      | Record<string, unknown>
      | undefined;
    const refs: ThreadRuntime["pending"] = [];
    if (sim && !sim.terminal)
      for (const id of sim.pendingIds) {
        const block = sim.events.find(
          (event) => event.event === "block" && (event.data as AgentUiBlock).id === id,
        )?.data as AgentUiBlock | undefined;
        if (!block) continue;
        const secret = block.type === "input" && (block.secret || block.inputKind === "password");
        const dialogue =
          block.type === "input" && !secret && !block.autocomplete && block.inputKind !== "code";
        refs.push({
          jobId: sim.jobId,
          blockId: id,
          secret,
          channel:
            block.type === "login"
              ? "manual"
              : block.type === "approval"
                ? "approval"
                : dialogue
                  ? "dialogue"
                  : "native",
        });
      }
    if (sim?.state === "waiting_user" && refs.length === 0) {
      const block = sim.events.findLast((event) => event.event === "block")?.data as AgentUiBlock | undefined;
      if (block?.type === "wait")
        refs.push({ jobId: sim.jobId, blockId: block.id, channel: "native", secret: false });
    }
    return {
      purpose: linked ? "connect" : "chat",
      connection: connected,
      activeJob: sim
        ? {
            id: sim.jobId,
            kind: sim.kind,
            connectionId: sim.connectionId,
            state: sim.state,
            stage: status?.stage ?? null,
            createdAt: iso(sim.startedAt || db.now()),
            startedAt: sim.startedAt ? iso(sim.startedAt) : null,
            finishedAt: sim.terminal ? iso() : null,
            result: done?.result ?? null,
            error: error ?? null,
          }
        : null,
      pending: refs,
      receipts: sims.flatMap((item) =>
        item.answeredIds.map((blockId) => ({
          jobId: item.jobId,
          blockId,
          messageId: null,
          status: "applied" as const,
          at: iso(),
        })),
      ),
    };
  };

  async function bootstrap(sim: JobSim, threadId: string, context?: ConnectContext, spaceId?: string | null) {
    try {
      const url =
        context?.entryUrls?.[0] ??
        (await sim.ask(
          {
            type: "input",
            id: ulid(),
            at: iso(),
            question: "Пришлите ссылку на источник, который хотите подключить.",
            inputKind: "url",
            secret: false,
            required: true,
            maxLength: 1000,
          },
          "Жду ссылку на источник",
        ));
      const university = context?.universityId
        ? db.universities.find((item) => item.id === context.universityId)
        : undefined;
      let space = db.spaces.find((item) => item.id === spaceId);
      if (!space) {
        space = {
          id: ulid(),
          universityId: university?.id ?? null,
          title: university?.title ?? "Мой вуз",
          accentHue: university?.accentHue ?? 325,
          timeZone: "Europe/Moscow",
          createdAt: iso(),
        };
        db.spaces.push(space);
      }
      const connection: S["ConnectionOut"] = {
        id: ulid(),
        spaceId: space.id,
        title: context?.title ?? "Личный кабинет",
        origin: typeof url === "string" ? url : "https://lk.fakeuni.test",
        kind: context?.kind ?? "portal",
        adapter: null,
        status: "learning",
        hasPassword: false,
        lastSyncAt: null,
        nextSyncAt: null,
        errorCode: null,
        errorDetail: null,
      };
      db.connections.push(connection);
      sim.connectionId = connection.id;
      const link = state().connections[threadId];
      if (link) link.connectionId = connection.id;
      const thread = find(threadId);
      if (thread) thread.spaceId = space.id;
      await runConnect(sim, db, connection.id, db.jobVariant);
    } catch (error) {
      if (!(error instanceof JobCancelledError)) throw error;
    } finally {
      delete state().busy[threadId];
      save();
    }
  }

  async function runReply(sim: JobSim, threadId: string, question: string): Promise<void> {
    const at = () => iso();
    const step = async (stage: string, ms = 600, detail?: string) => {
      const id = ulid();
      sim.status(stage);
      sim.block({ type: "thinking", id, at: at(), stage, state: "running", detail });
      await sim.pause(ms);
      sim.update(id, { state: "ok" });
    };
    let finalText = "";
    let sources: Source[] = [];
    let usage = { data: 0, documents: 0 };
    try {
      sim.status("Читаю вопрос");
      await sim.pause(300);
      const intent = intentOf(question);
      const connection = db.connections.find((item) => item.kind === "portal") ?? db.connections[0];
      const fetchedAt = iso(db.now() - 12 * 60_000);

      if (intent === "failure") {
        await step("Ищу в подключённых источниках", 400);
        sim.emit("error", {
          type: "https://botai.mnema.app/errors/quota_exceeded",
          title: "quota_exceeded",
          status: 429,
          code: "quota_exceeded",
          detail: "",
        });
        return;
      }

      if (intent === "schedule") {
        await step("Ищу расписание в подключённых источниках");
        await step("Читаю расписание на сегодня", 700);
        const now = db.now();
        const dayStart = startOfDay(now, MOCK_ZONE);
        const today = seedEvents(db)
          .filter((event) => {
            const start = Date.parse(event.startsAt);
            return start >= dayStart && start < addDays(dayStart, 1, MOCK_ZONE);
          })
          .sort((a, b) => Date.parse(a.startsAt) - Date.parse(b.startsAt));
        const next = today.find((event) => Date.parse(event.startsAt) > now);
        const last = today.at(-1);
        if (!connection || today.length === 0) {
          finalText =
            "Не знаю: в подключённых источниках нет занятий на сегодня. Подключите вуз или проверьте, что расписание обновилось.";
        } else {
          finalText = next
            ? `Следующая пара — **${next.title}** в ${clock(Date.parse(next.startsAt))}${next.location ? `, ${next.location}` : ""} [1].\n\nВсего сегодня пар: ${today.length} [1][2].`
            : `На сегодня пары закончились. Последняя — **${last?.title}** в ${clock(Date.parse(last?.startsAt ?? ""))} [1].`;
          sources = [
            { title: "Расписание — личный кабинет", url: `botai://connections/${connection.id}`, fetchedAt },
            {
              title: "Расписание на сайте вуза",
              url: (next ?? last)?.url ?? "https://lk.fakeuni.test/timetable",
              fetchedAt,
            },
          ];
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
                return `- **${row.title}** — ${due === null ? "срок не указан" : `до ${dayMonth(due)}, ${clock(due)}`} [1]`;
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
          finalText = `Об этом написано в документе «${doc.title}» [1]. Курсовую работу сдают через личный кабинет до начала сессии.`;
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
              expiresAt: iso(db.now() + 600_000),
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

      const block = textBlock(finalText, at(), sources);
      sim.block(block);
      const message: Message = {
        id: ulid(),
        role: "agent",
        blocks: [block as never],
        jobId: sim.jobId,
        createdAt: at(),
      };
      const chat = state();
      if (
        !chat.messages[threadId]?.some((item) => item.blocks.some((candidate) => candidate.id === block.id))
      )
        chat.messages[threadId] = [...(chat.messages[threadId] ?? []), message];
      chat.last[threadId] = { ...usage, sources };
      const thread = find(threadId);
      if (thread) {
        thread.updatedAt = message.createdAt;
        if (thread.title === DEFAULT_TITLE) {
          const line = question.replace(/\s+/g, " ").trim();
          thread.title = line.length > 60 ? `${line.slice(0, 57)}…` : line || DEFAULT_TITLE;
        }
      }
      sim.status("Готово");
      sim.emit("done", {
        result: {
          messageId: message.id,
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
      save();
    }
  }

  return [
    http.get("*/api/v1/chat/threads", async () => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const rows = [...state().threads].sort(
        (a, b) => b.updatedAt.localeCompare(a.updatedAt) || b.id.localeCompare(a.id),
      );
      return HttpResponse.json(rows);
    }),

    http.post("*/api/v1/chat/threads", async ({ request }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const body = (await request.json().catch(() => ({}))) as S["ThreadCreate"] & {
        purpose?: "chat" | "connect";
        clientRequestId?: string;
        connectContext?: ConnectContext;
      };
      const previous = body.clientRequestId ? state().requests[body.clientRequestId] : undefined;
      if (previous) return HttpResponse.json(find(previous), { status: 201 });
      if (body.spaceId && !db.spaces.some((space) => space.id === body.spaceId))
        return problem(404, "not_found", "Пространство не найдено");
      const now = iso();
      const thread: Thread = {
        id: ulid(),
        spaceId: body.spaceId ?? null,
        title:
          (body.title ?? (body.purpose === "connect" ? "Подключение источника" : DEFAULT_TITLE)).trim() ||
          DEFAULT_TITLE,
        createdAt: now,
        updatedAt: now,
      };
      state().threads.push(thread);
      state().messages[thread.id] = [];
      if (body.clientRequestId) state().requests[body.clientRequestId] = thread.id;
      if (body.purpose === "connect") {
        const sim = new JobSim(ulid(), db.now, jobPace, null, "connect");
        db.sims.set(sim.jobId, sim);
        state().connections[thread.id] = { connectionId: null, jobs: [sim.jobId] };
        state().busy[thread.id] = sim.jobId;
        observe(sim, thread.id);
        void bootstrap(sim, thread.id, body.connectContext, body.spaceId);
      }
      save();
      return HttpResponse.json(thread, { status: 201 });
    }),

    http.get("*/api/v1/chat/threads/:threadId/runtime", async ({ params }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const id = String(params.threadId);
      if (!find(id)) return problem(404, "not_found", "Чат не найден");
      return HttpResponse.json(runtimeFor(id));
    }),

    http.post("*/api/v1/connections/:connectionId/conversation", async ({ params, request }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const connectionId = String(params.connectionId);
      const connection = db.connections.find((item) => item.id === connectionId);
      if (!connection) return problem(404, "not_found", "Подключение не найдено");
      const { jobId } = (await request.json().catch(() => ({}))) as { jobId?: string };
      const sim = jobId
        ? db.sims.get(jobId)
        : [...db.sims.values()].findLast((item) => item.connectionId === connectionId);
      if (jobId && (!sim || sim.connectionId !== connectionId))
        return problem(404, "not_found", "Задание не найдено");
      const existing = Object.entries(state().connections).find(
        ([, item]) => item.connectionId === connectionId,
      )?.[0];
      const thread: Thread = existing
        ? (find(existing) as Thread)
        : {
            id: ulid(),
            spaceId: connection.spaceId,
            title: connection.title,
            createdAt: iso(),
            updatedAt: iso(),
          };
      if (!existing) {
        state().threads.push(thread);
        state().messages[thread.id] = [];
        state().connections[thread.id] = { connectionId, jobs: [] };
      }
      const link = state().connections[thread.id];
      if (sim && link && !link.jobs.includes(sim.jobId)) {
        link.jobs.push(sim.jobId);
        observe(sim, thread.id);
      }
      save();
      return HttpResponse.json(thread);
    }),

    http.get("*/api/v1/chat/threads/:threadId/messages", async ({ params }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const id = String(params.threadId);
      if (!find(id)) return problem(404, "not_found", "Чат не найден");
      return HttpResponse.json(state().messages[id] ?? []);
    }),

    http.post("*/api/v1/chat/threads/:threadId/messages", async ({ params, request }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const id = String(params.threadId);
      const thread = find(id);
      if (!thread) return problem(404, "not_found", "Чат не найден");
      const body = (await request.json().catch(() => ({}))) as Partial<S["MessageIn"]> & {
        targetJobId?: string;
        clientMessageId?: string;
      };
      const text = typeof body.text === "string" ? body.text : "";
      if (text.length < 1 || text.length > 8000)
        return problem(422, "validation", "Сообщение пустое или слишком длинное");
      const acceptedKey = body.clientMessageId ? `${id}:${body.clientMessageId}` : null;
      if (acceptedKey && state().accepted[acceptedKey])
        return HttpResponse.json(state().accepted[acceptedKey], { status: 202 });
      const runtime = runtimeFor(id);
      const source =
        runtime.purpose === "connect" &&
        runtime.activeJob &&
        runtime.activeJob.kind !== "chat" &&
        !["done", "failed", "cancelled"].includes(runtime.activeJob.state)
          ? db.sims.get(runtime.activeJob.id)
          : undefined;
      if (body.targetJobId && body.targetJobId !== source?.jobId)
        return problem(409, "conflict", "Этот вопрос уже закрыт");
      if (source && runtime.pending.some((item) => item.channel !== "dialogue" || item.secret))
        return problem(409, "conflict", "Ответьте в защищённом блоке");
      if (!source && state().busy[id])
        return problem(409, "conflict", "Ассистент ещё отвечает на прошлое сообщение");
      const now = iso();
      const message: Message = {
        id: ulid(),
        role: "user",
        blocks: [textBlock(text, now) as never],
        jobId: null,
        createdAt: now,
      };
      state().messages[id] = [...(state().messages[id] ?? []), message];
      thread.updatedAt = now;
      if (source) {
        const accepted = { message, jobId: source.jobId } satisfies S["MessageAccepted"];
        if (acceptedKey) state().accepted[acceptedKey] = accepted;
        const request = runtime.pending.find((item) => item.channel === "dialogue");
        if (request) {
          const block = source.events.find(
            (event) => event.event === "block" && (event.data as AgentUiBlock).id === request.blockId,
          )?.data as AgentUiBlock | undefined;
          if (block?.type === "input" && block.inputKind === "url" && !/^https?:\/\//.test(text))
            source.block(textBlock("Уточнение получено. Пришлите ссылку, когда будете готовы.", now));
          else source.answer(request.blockId, text, false);
        }
        save();
        return HttpResponse.json(accepted, { status: 202 });
      }
      const jobId = ulid();
      const sim = new JobSim(jobId, db.now, jobPace);
      db.sims.set(jobId, sim);
      if (state().connections[id]) observe(sim, id);
      state().busy[id] = jobId;
      save();
      void runReply(sim, id, text);
      const accepted = { message, jobId } satisfies S["MessageAccepted"];
      if (acceptedKey) state().accepted[acceptedKey] = accepted;
      return HttpResponse.json(accepted, { status: 202 });
    }),

    http.get("*/api/v1/chat/threads/:threadId/context", async ({ params }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const id = String(params.threadId);
      if (!find(id)) return problem(404, "not_found", "Чат не найден");
      const last = state().last[id] ?? { data: 0, documents: 0, sources: [] };
      const history = estimate(
        (state().messages[id] ?? [])
          .flatMap((message) =>
            message.blocks.map((block) => String((block as { markdown?: string }).markdown ?? "")),
          )
          .join(" "),
      );
      const tokens = { system: 9_800, profile: 600, data: last.data, documents: last.documents, history };
      const used = Object.values(tokens).reduce((sum, value) => sum + value, 0);
      const labels: Record<string, string> = {
        system: "Правила и навыки",
        profile: "Профиль",
        data: "Данные вуза",
        documents: "Документы",
        history: "Переписка",
      };
      const body: S["ContextOut"] = {
        limit: CONTEXT_LIMIT,
        used,
        segments: [
          ...Object.entries(tokens).map(([kind, count]) => ({
            kind: kind as S["ContextSegment"]["kind"],
            label: labels[kind] ?? kind,
            tokens: count,
          })),
          { kind: "free", label: "Свободно", tokens: Math.max(CONTEXT_LIMIT - used, 0) },
        ],
        sources: last.sources.map((source) => ({
          title: source.title,
          kind: source.url.startsWith("botai://documents") ? "document" : "data",
          fetchedAt: source.fetchedAt,
        })),
      };
      return HttpResponse.json(body);
    }),
  ];
}

/** Для тестов: сбросить состояние чата базы. */
export function resetChatState(db: MockDb): void {
  states.delete(db);
}

/**
 * Обработчики MSW для чата агент-хаба v3: треды (непрочитанное, внимание, активная задача), сообщения с вложениями,
 * состояние треда (`runtime`), вложения и шкала контекста. Поток заданий (`GET /jobs/{id}/events`) отдают общие
 * обработчики заданий. Форма — по плану `docs/plan/agent-hub-v3.md` §3.2, §3.6; «агент» мока — `chat-agent.ts`.
 */
import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import {
  type AttachmentOut,
  isTaskKind,
  type JobOut,
  type MessageAccepted,
  type MessageIn,
  type ThreadCreateIn,
  type ThreadRuntime,
} from "@/api/v3";
import { type AgentCtx, observe, runReply } from "./chat-agent";
import {
  asStored,
  attachmentBytes,
  type ChatState,
  DEFAULT_TITLE,
  loadState,
  type Message,
  type MockAttachment,
  resetChatState,
  saveState,
  textBlock,
  threadOut,
} from "./chat-state";
import { findUser, type MockDb, ulid } from "./db";
import { JobSim } from "./job-sim";

type S = components["schemas"];

export { resetChatState };

export interface ChatHandlerOptions {
  latency?: number;
  persist?: boolean;
  /** Множитель пауз ответа агента: 0 — мгновенно (тесты). */
  jobPace?: number;
}

const CONTEXT_LIMIT = 1_000_000;
const MAX_FILE_BYTES = 20 * 1024 * 1024;

/** Что «услышал» стенд на любой записи: фраза из примеров чата. Ошибки сервиса в тестах подменяются своим обработчиком. */
export const MOCK_DICTATION = "Какие у меня пары завтра";

/** Грубая копия серверной защиты: слово без пробелов с цифрой или символом внутри, строка из цифр, «пароль: …». */
function looksLikeSecret(text: string): boolean {
  const value = text.trim();
  if (!value) return false;
  if (/(?:парол\w*|password|код\w*)\s*[:=]?\s*\S+/i.test(value) && /\d{4}|[:=]/.test(value)) return true;
  if (/^[\d -]+$/.test(value)) return value.replace(/\D/g, "").length >= 4;
  if (value.length < 4 || value.length > 128 || /\s/.test(value)) return false;
  return /\d/.test(value) || /\w[^\w\s]\w/u.test(value) || /[a-zа-яё][A-ZА-ЯЁ]/u.test(value);
}

function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

interface Upload {
  file: { name: string; type: string; size: number };
  data: ArrayBuffer;
  threadId: string;
}

/**
 * Разбор multipart-тела загрузки. В браузере это `request.formData()`; окружение тестов (jsdom поверх Node) не
 * разбирает чужой multipart, и тогда тело читается как текст: имя, тип и размер те же, содержимое — для проверки.
 */
async function readUpload(request: Request, field = "file"): Promise<Upload | null> {
  try {
    const form = await request.clone().formData();
    const entry = form.get(field);
    if (!entry || typeof entry === "string") return null;
    const file = entry as File;
    const threadId = form.get("threadId");
    return {
      file: { name: file.name, type: file.type, size: file.size },
      data: await file.arrayBuffer(),
      threadId: typeof threadId === "string" ? threadId : "",
    };
  } catch {
    const boundary = /boundary=([^;]+)/.exec(request.headers.get("content-type") ?? "")?.[1];
    if (!boundary) return null;
    const text = await request.text();
    let result: Upload | null = null;
    let threadId = "";
    for (const part of text.split(`--${boundary}`)) {
      const split = part.indexOf("\r\n\r\n");
      if (split < 0) continue;
      const head = part.slice(0, split);
      const body = part.slice(split + 4).replace(/\r\n$/, "");
      const name = /name="([^"]*)"/.exec(head)?.[1];
      const filename = /filename="([^"]*)"/.exec(head)?.[1];
      if (name === "threadId") threadId = body;
      else if (name === field && filename !== undefined) {
        const bytes = new TextEncoder().encode(body);
        result = {
          file: {
            name: decodeURIComponent(filename),
            type: /Content-Type:\s*([^\r\n]+)/i.exec(head)?.[1] ?? "",
            size: bytes.byteLength,
          },
          data: bytes.buffer as ArrayBuffer,
          threadId,
        };
      }
    }
    return result ? { ...result, threadId } : null;
  }
}

const estimate = (text: string) => Math.ceil(text.length / 3);

function kindOfMime(mime: string, name: string): MockAttachment["kind"] {
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("audio/")) return "audio";
  if (
    /^(application\/(pdf|msword|vnd\.)|text\/)/.test(mime) ||
    /\.(pdf|docx?|xlsx?|csv|tsv|txt|md|html?)$/i.test(name)
  )
    return "document";
  return "other";
}

const attachmentOut = (item: MockAttachment): AttachmentOut => ({
  id: item.id,
  name: item.name,
  mime: item.mime,
  size: item.size,
  kind: item.kind,
  createdAt: item.createdAt,
  documentId: null,
  ingest: item.kind === "document" ? "indexed" : "none",
});

export function createChatHandlers(db: MockDb, options: ChatHandlerOptions = {}) {
  const { latency = 0, persist = false, jobPace = 0 } = options;
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const user = () => findUser(db, db.sessionUserId);
  const state = () => loadState(db, persist);
  const save = () => saveState(db, persist);
  const iso = (ms = db.now()) => new Date(ms).toISOString();
  const find = (threadId: string) => state().threads.find((item) => item.id === threadId);
  const ctx: AgentCtx = { db, state, save, iso, jobPace, find };

  const simsOf = (threadId: string): JobSim[] =>
    (state().jobs[threadId] ?? []).map((id) => db.sims.get(id)).filter((sim): sim is JobSim => Boolean(sim));

  const jobOut = (sim: JobSim): JobOut => {
    const status = sim.events.findLast((event) => event.event === "status")?.data as
      | { stage?: string }
      | undefined;
    const done = sim.events.findLast((event) => event.event === "done")?.data as
      | { result?: Record<string, unknown> }
      | undefined;
    const error = sim.events.findLast((event) => event.event === "error")?.data as
      | Record<string, unknown>
      | undefined;
    return {
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
    };
  };

  const runtimeFor = (threadId: string): ThreadRuntime => {
    const sims = simsOf(threadId);
    const live = sims.filter((sim) => !sim.terminal);
    // Ход агента важнее фоновой задачи: он и есть то, что человек ждёт; без работы — последнее задание.
    const active = live.findLast((sim) => sim.kind === "chat") ?? live.at(-1) ?? sims.at(-1);
    const pending: ThreadRuntime["pending"] = [];
    for (const sim of live) {
      for (const id of sim.pendingIds) {
        const block = sim.events.find(
          (event) => event.event === "block" && (event.data as { id?: string }).id === id,
        )?.data as { type?: string; secret?: boolean; inputKind?: string; autocomplete?: string } | undefined;
        if (!block) continue;
        const secret = block.type === "input" && (block.secret === true || block.inputKind === "password");
        const dialogue =
          block.type === "input" && !secret && !block.autocomplete && block.inputKind !== "code";
        pending.push({
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
      if (sim.state === "waiting_user" && !pending.some((item) => item.jobId === sim.jobId)) {
        const block = sim.events.findLast((event) => event.event === "block")?.data as
          | { type?: string; id?: string }
          | undefined;
        if (block?.type === "wait" && block.id)
          pending.push({ jobId: sim.jobId, blockId: block.id, channel: "native", secret: false });
      }
    }
    return {
      activeJob: active ? jobOut(active) : null,
      tasks: sims.filter((sim) => isTaskKind(sim.kind) && !sim.terminal).map(jobOut),
      pending,
      receipts: sims.flatMap((sim) =>
        sim.answeredIds.map((blockId) => ({
          jobId: sim.jobId,
          blockId,
          messageId: null,
          status: "applied" as const,
          at: iso(),
        })),
      ),
    };
  };

  return [
    http.get("*/api/v1/chat/threads", async () => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const rows = state()
        .threads.map((thread) => threadOut(db, state(), thread))
        .sort((a, b) => b.lastMessageAt.localeCompare(a.lastMessageAt) || b.id.localeCompare(a.id));
      return HttpResponse.json(rows);
    }),

    http.post("*/api/v1/chat/threads", async ({ request }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const body = (await request.json().catch(() => ({}))) as ThreadCreateIn;
      const previous = body.clientRequestId ? state().requests[body.clientRequestId] : undefined;
      const known = previous ? find(previous) : undefined;
      if (known) return HttpResponse.json(threadOut(db, state(), known), { status: 201 });
      if (body.spaceId && !db.spaces.some((space) => space.id === body.spaceId))
        return problem(404, "not_found", "Пространство не найдено");
      const now = iso();
      const thread = {
        id: ulid(),
        spaceId: body.spaceId ?? null,
        title: (body.title ?? DEFAULT_TITLE).trim() || DEFAULT_TITLE,
        createdAt: now,
        lastMessageAt: now,
        readCount: 0,
      };
      state().threads.push(thread);
      state().messages[thread.id] = [];
      state().jobs[thread.id] = [];
      if (body.clientRequestId) state().requests[body.clientRequestId] = thread.id;
      save();
      return HttpResponse.json(threadOut(db, state(), thread), { status: 201 });
    }),

    http.patch("*/api/v1/chat/threads/:threadId", async ({ params, request }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const thread = find(String(params.threadId));
      if (!thread) return problem(404, "not_found", "Чат не найден");
      const body = (await request.json().catch(() => ({}))) as { title?: string };
      const title = typeof body.title === "string" ? body.title.trim() : "";
      if (!title || title.length > 120)
        return problem(422, "validation", "Название пустое или слишком длинное");
      thread.title = title;
      save();
      return HttpResponse.json(threadOut(db, state(), thread));
    }),

    http.delete("*/api/v1/chat/threads/:threadId", async ({ params }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const id = String(params.threadId);
      if (!find(id)) return problem(404, "not_found", "Чат не найден");
      // Удаление останавливает задачи треда.
      for (const sim of simsOf(id)) sim.cancel();
      const chat = state();
      chat.threads = chat.threads.filter((thread) => thread.id !== id);
      delete chat.messages[id];
      delete chat.jobs[id];
      delete chat.busy[id];
      save();
      return new HttpResponse(null, { status: 204 });
    }),

    http.post("*/api/v1/chat/threads/:threadId/read", async ({ params, request }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const thread = find(String(params.threadId));
      if (!thread) return problem(404, "not_found", "Чат не найден");
      // Как у сервера: `upTo` — время последнего показанного сообщения, без тела прочитано всё написанное.
      const { upTo } = (await request.json().catch(() => ({}))) as { upTo?: string };
      const shown = upTo ? Date.parse(upTo) : Number.NaN;
      const all = state().messages[thread.id] ?? [];
      const count = Number.isNaN(shown)
        ? all.length
        : all.filter((message) => Date.parse(message.createdAt) <= shown).length;
      thread.readCount = Math.max(thread.readCount, count);
      save();
      return new HttpResponse(null, { status: 204 });
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
      const known = (connection as { threadId?: string }).threadId;
      let thread = known ? find(known) : undefined;
      if (!thread) {
        const now = iso();
        thread = {
          id: ulid(),
          spaceId: connection.spaceId,
          title: connection.title,
          createdAt: now,
          lastMessageAt: now,
          readCount: 0,
        };
        state().threads.push(thread);
        state().messages[thread.id] = [];
        state().jobs[thread.id] = [];
        (connection as { threadId?: string }).threadId = thread.id;
      }
      const jobs = state().jobs[thread.id] ?? [];
      if (sim && !jobs.includes(sim.jobId)) {
        state().jobs[thread.id] = [...jobs, sim.jobId];
        observe(ctx, sim, thread.id);
      }
      save();
      return HttpResponse.json(threadOut(db, state(), thread));
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
      const body = (await request.json().catch(() => ({}))) as Partial<MessageIn>;
      const text = typeof body.text === "string" ? body.text : "";
      const ids = Array.isArray(body.attachmentIds) ? body.attachmentIds : [];
      if (text.length > 8000 || ids.length > 10 || (text.length < 1 && ids.length === 0))
        return problem(422, "validation", "Сообщение пустое или слишком длинное");
      const files = ids.map((fileId) => state().attachments[fileId]);
      if (files.some((file) => !file)) return problem(404, "not_found", "Вложение не найдено");
      if (files.some((file) => file?.attached))
        return problem(409, "conflict", "Файл уже отправлен в другом сообщении");
      const acceptedKey = body.clientMessageId ? `${id}:${body.clientMessageId}` : null;
      const again = acceptedKey ? state().accepted[acceptedKey] : undefined;
      if (again) return HttpResponse.json(again satisfies MessageAccepted, { status: 202 });

      const runtime = runtimeFor(id);
      // Файлы идут агенту, а не ответом на вопрос задачи (как на сервере).
      if (body.targetJobId && ids.length > 0)
        return problem(422, "validation", "Файлы отправляются обычным сообщением в чат");
      const dialogue =
        ids.length > 0
          ? undefined
          : runtime.pending.find((item) => item.channel === "dialogue" && !item.secret);
      if (body.targetJobId && body.targetJobId !== dialogue?.jobId)
        return problem(409, "stale_target", "Этот шаг уже завершён. Откройте текущее состояние разговора");
      const secretOpen = runtime.pending.some((item) => item.channel === "native" && item.secret);
      if (secretOpen && looksLikeSecret(text))
        return problem(409, "secret_in_chat", "Пароль и код вводятся только в защищённое поле, не в чат");
      const turn = state().busy[id];
      const now = iso();
      const attached = files.filter((file): file is MockAttachment => Boolean(file));
      // Порядок сервера: текст, затем вложения.
      const blocks: Message["blocks"] = [
        ...(text ? [asStored(textBlock(text, now))] : []),
        ...attached.map((file) => ({
          type: "attachment",
          id: ulid(),
          at: now,
          attachmentId: file.id,
          name: file.name,
          mime: file.mime,
          size: file.size,
          kind: file.kind,
        })),
      ];

      // Несекретный вопрос задачи, ждущий ответа в диалоге, получает сообщение; иначе отвечает агент.
      const task = dialogue ? db.sims.get(dialogue.jobId) : undefined;
      if (!task && turn) return problem(409, "agent_busy", "Ассистент ещё отвечает на прошлое сообщение");
      const message: Message = {
        id: ulid(),
        role: "user",
        blocks,
        jobId: null,
        createdAt: now,
        clientMessageId: body.clientMessageId ?? null,
      };
      state().messages[id] = [...(state().messages[id] ?? []), message];
      thread.lastMessageAt = now;
      for (const file of attached) {
        file.attached = true;
        file.threadId = id;
      }

      if (task && dialogue) {
        const accepted = { message, jobId: task.jobId } satisfies MessageAccepted;
        if (acceptedKey) state().accepted[acceptedKey] = accepted;
        const asked = task.events.find(
          (event) => event.event === "block" && (event.data as { id?: string }).id === dialogue.blockId,
        )?.data as { type?: string; inputKind?: string } | undefined;
        if (asked?.type === "input" && asked.inputKind === "url" && !/^https?:\/\//.test(text))
          task.block(textBlock("Уточнение получено. Пришлите ссылку, когда будете готовы.", now));
        else task.answer(dialogue.blockId, text, false);
        save();
        return HttpResponse.json(accepted, { status: 202 });
      }

      const jobId = ulid();
      const sim = new JobSim(jobId, db.now, jobPace);
      db.sims.set(jobId, sim);
      state().jobs[id] = [...(state().jobs[id] ?? []), jobId];
      observe(ctx, sim, id);
      state().busy[id] = jobId;
      const accepted = { message, jobId } satisfies MessageAccepted;
      if (acceptedKey) state().accepted[acceptedKey] = accepted;
      save();
      void runReply(ctx, sim, id, { question: text, files: attached });
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

    // ---------- Вложения (§3.6) ----------

    http.post("*/api/v1/attachments", async ({ request }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const upload = await readUpload(request);
      if (!upload) return problem(422, "validation", "Файл не передан");
      const file = upload.file;
      // undici (Node на Linux) кодирует имя файла в multipart процентами; браузер шлёт UTF-8 как есть.
      const name = decodedName(file.name);
      if (file.size > MAX_FILE_BYTES) return problem(413, "file_too_large", "Файл больше 20 МБ");
      if (file.type === "application/x-msdownload" || /\.(exe|bat|dll)$/i.test(name))
        return problem(415, "unsupported_type", "Такой тип файла не поддерживается");
      const threadId = upload.threadId;
      const item: MockAttachment = {
        id: ulid(),
        name,
        mime: file.type || "application/octet-stream",
        size: file.size,
        kind: kindOfMime(file.type, name),
        createdAt: iso(),
        threadId: threadId || null,
        attached: false,
      };
      state().attachments[item.id] = item;
      attachmentBytes(db).set(item.id, { data: upload.data, mime: item.mime });
      save();
      return HttpResponse.json(attachmentOut(item), { status: 201 });
    }),

    http.get("*/api/v1/attachments/:attachmentId", async ({ params }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const item = state().attachments[String(params.attachmentId)];
      return item ? HttpResponse.json(attachmentOut(item)) : problem(404, "not_found", "Вложение не найдено");
    }),

    http.get("*/api/v1/attachments/:attachmentId/content", async ({ params }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const id = String(params.attachmentId);
      const item = state().attachments[id];
      const stored = attachmentBytes(db).get(id);
      if (!item || !stored) return problem(404, "not_found", "Вложение не найдено");
      return new HttpResponse(stored.data, {
        headers: {
          "content-type": item.mime,
          "content-disposition": `inline; filename*=UTF-8''${encodeURIComponent(item.name)}`,
          "cache-control": "private, no-store",
        },
      });
    }),

    http.delete("*/api/v1/attachments/:attachmentId", async ({ params }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const id = String(params.attachmentId);
      const item = state().attachments[id];
      if (!item) return problem(404, "not_found", "Вложение не найдено");
      if (item.attached) return problem(409, "conflict", "Вложение уже отправлено");
      delete state().attachments[id];
      attachmentBytes(db).delete(id);
      save();
      return new HttpResponse(null, { status: 204 });
    }),

    // ---------- Диктовка: запись → текст для поля ввода ----------

    http.post("*/api/v1/speech/transcribe", async ({ request }) => {
      await wait();
      if (!user()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const upload = await readUpload(request, "audio");
      if (!upload || upload.file.size === 0)
        return problem(422, "validation", "Запись пустая. Запишите ещё раз.");
      return HttpResponse.json({ text: MOCK_DICTATION });
    }),
  ];
}

export type { ChatState };

function decodedName(name: string): string {
  if (!/%[0-9A-Fa-f]{2}/.test(name)) return name;
  try {
    return decodeURIComponent(name);
  } catch {
    return name;
  }
}

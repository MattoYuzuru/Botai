/**
 * Обработчики MSW для экранов данных: «Сегодня», расписание, задания, «Учёба» (предметы, оценки, люди,
 * новости, документы), управление подключениями. Форма ответов — по OpenAPI бэкенда; эндпоинтов, которых
 * там нет, здесь нет. Данные — `data-seed.ts`.
 *
 * Для проверки состояний данных адрес принимает `?data=`: `quiet` (вузы подключены, данных нет),
 * `repairing`, `waiting`, `error` — состояние одного из подключений. Значение запоминается на вкладку.
 */
import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import { addDays, startOfDay } from "@/features/data/dates";
import {
  dueOf,
  MOCK_ZONE,
  seedCourses,
  seedDeadlines,
  seedDocuments,
  seedEvents,
  seedGrades,
  seedNews,
  seedPeople,
  seedSourceOf,
} from "./data-seed";
import { findUser, type MockDb, persistDb, ulid } from "./db";

type S = components["schemas"];

export interface DataHandlerOptions {
  latency?: number;
  persist?: boolean;
  /** Подмена варианта данных (тесты); в браузере берётся из адреса. */
  variant?: DataVariant | null;
}

export type DataVariant = "quiet" | "repairing" | "waiting" | "error";
const VARIANTS: readonly DataVariant[] = ["quiet", "repairing", "waiting", "error"];
const STORAGE_KEY = "botai.mock.data";

function readVariant(): DataVariant | null {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get("data");
    if (fromUrl && (VARIANTS as readonly string[]).includes(fromUrl)) {
      window.sessionStorage.setItem(STORAGE_KEY, fromUrl);
      return fromUrl as DataVariant;
    }
    const stored = window.sessionStorage.getItem(STORAGE_KEY);
    return stored && (VARIANTS as readonly string[]).includes(stored) ? (stored as DataVariant) : null;
  } catch {
    return null;
  }
}

function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const ATTENTION_STATUSES = new Set<S["ConnectionOut"]["status"]>([
  "stale",
  "needs_reauth",
  "waiting_user",
  "repairing",
  "error",
]);
const DONE = new Set<S["DeadlineOut"]["state"]>(["submitted", "graded"]);
const ALLOWED_UPLOADS = /\.(pdf|docx|xlsx|csv|html?|txt|md)$/i;
export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

interface MockJob {
  id: string;
  connectionId: string;
  kind: "ingest" | "sync";
  title: string;
  polls: number;
  createdAt: string;
  /** Документ, который появится, когда задание завершится. */
  document?: S["DocumentOut"];
}

/** Применяет вариант данных к подключениям: так проверяются состояния `repairing`, `reauth` и `error`. */
function applyVariant(db: MockDb, variant: DataVariant | null) {
  if (!variant || variant === "quiet") return;
  const target = db.connections.find((c) => c.kind === "portal") ?? db.connections[0];
  if (!target) return;
  if (variant === "repairing") target.status = "repairing";
  if (variant === "waiting") target.status = "waiting_user";
  if (variant === "error") {
    target.status = "error";
    target.errorCode = "source_unavailable";
    target.errorDetail = "Сайт вуза не отвечает уже вторые сутки.";
  }
}

export function createDataHandlers(db: MockDb, options: DataHandlerOptions = {}) {
  const { latency = 0, persist = false } = options;
  const variant = options.variant === undefined ? readVariant() : options.variant;
  applyVariant(db, variant);
  const quiet = variant === "quiet";

  const jobs = new Map<string, MockJob>();
  let documents: S["DocumentOut"][] | null = null;
  const docs = () => {
    documents ??= seedDocuments(db);
    return documents;
  };

  const save = () => {
    if (persist) persistDb(db);
  };
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const signedIn = () => Boolean(findUser(db, db.sessionUserId));
  const unauthorized = () => problem(401, "unauthorized", "Нужно войти в BotAI");
  const hasSpace = (id: string) => db.spaces.some((space) => space.id === id);
  const spaceFilter = (url: URL) => url.searchParams.get("space_id");

  /** Список домена с фильтром по пространству: чужой id — 404, как у сервера; отключённые источники не в счёт. */
  const listHandler = <T extends { provenance: S["Provenance"] }>(path: string, rows: () => T[]) =>
    http.get(path, async ({ request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const spaceId = spaceFilter(new URL(request.url));
      if (spaceId && !hasSpace(spaceId)) return problem(404, "not_found", "Пространство не найдено");
      if (quiet) return HttpResponse.json([]);
      const all = rows().filter((row) => db.connections.some((c) => c.id === row.provenance.connectionId));
      return HttpResponse.json(spaceId ? all.filter((row) => row.provenance.spaceId === spaceId) : all);
    });

  const startJob = (job: Omit<MockJob, "polls" | "createdAt" | "id">) => {
    const id = ulid();
    jobs.set(id, { ...job, id, polls: 0, createdAt: new Date(db.now()).toISOString() });
    return id;
  };

  return [
    http.get("*/api/v1/today", async () => {
      await wait();
      if (!signedIn()) return unauthorized();
      const now = db.now();
      const from = startOfDay(now, MOCK_ZONE);
      const to = addDays(now, 1, MOCK_ZONE);
      const live = new Set(db.connections.filter((c) => c.status !== "disabled").map((c) => c.id));
      const events = quiet
        ? []
        : seedEvents(db).filter(
            (e) =>
              live.has(e.provenance.connectionId) &&
              Date.parse(e.startsAt) < to &&
              Date.parse(e.endsAt) > from,
          );
      const deadlines = quiet
        ? []
        : seedDeadlines(db)
            .filter((d) => live.has(d.provenance.connectionId) && !DONE.has(d.state))
            .filter((d) => {
              const due = dueOf(d);
              return due !== null && due >= from;
            })
            .sort((a, b) => (dueOf(a) ?? 0) - (dueOf(b) ?? 0))
            .slice(0, 10);
      const attention = db.connections
        .filter((c) => c.status !== "disabled")
        .filter((c) => {
          const last = c.lastSyncAt ? Date.parse(c.lastSyncAt) : Number.NaN;
          return ATTENTION_STATUSES.has(c.status) || (Number.isFinite(last) && now - last > 24 * 3_600_000);
        })
        .map((c) => c.id);
      const body: S["TodayOut"] = {
        date: new Date(from + 12 * 3_600_000).toISOString().slice(0, 10),
        events,
        deadlines,
        attention,
      };
      return HttpResponse.json(body);
    }),

    http.get("*/api/v1/schedule", async ({ request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const url = new URL(request.url);
      const start = Date.parse(url.searchParams.get("start") ?? "");
      const end = Date.parse(url.searchParams.get("end") ?? "");
      if (!Number.isFinite(start) || !Number.isFinite(end))
        return problem(422, "validation", "Укажите начало и конец периода");
      if (end <= start) return problem(422, "validation", "Конец периода должен быть позже начала");
      const spaceId = spaceFilter(url);
      if (spaceId && !hasSpace(spaceId)) return problem(404, "not_found", "Пространство не найдено");
      if (quiet) return HttpResponse.json([]);
      const live = new Set(db.connections.filter((c) => c.status !== "disabled").map((c) => c.id));
      const rows = seedEvents(db).filter(
        (e) =>
          live.has(e.provenance.connectionId) &&
          Date.parse(e.startsAt) < end &&
          Date.parse(e.endsAt) > start &&
          (!spaceId || e.provenance.spaceId === spaceId),
      );
      return HttpResponse.json(rows);
    }),

    http.get("*/api/v1/deadlines", async ({ request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const url = new URL(request.url);
      const spaceId = spaceFilter(url);
      if (spaceId && !hasSpace(spaceId)) return problem(404, "not_found", "Пространство не найдено");
      if (quiet) return HttpResponse.json([]);
      const includeDone = url.searchParams.get("include_done") === "true";
      const live = new Set(db.connections.filter((c) => c.status !== "disabled").map((c) => c.id));
      const rows = seedDeadlines(db)
        .filter((d) => live.has(d.provenance.connectionId))
        .filter((d) => (spaceId ? d.provenance.spaceId === spaceId : true))
        .filter((d) => includeDone || !DONE.has(d.state))
        .sort((a, b) => (dueOf(a) ?? Number.POSITIVE_INFINITY) - (dueOf(b) ?? Number.POSITIVE_INFINITY));
      return HttpResponse.json(rows);
    }),

    listHandler("*/api/v1/courses", () => seedCourses(db)),
    listHandler("*/api/v1/grades", () => seedGrades(db)),
    listHandler("*/api/v1/people", () => seedPeople(db)),
    listHandler("*/api/v1/news", () => seedNews(db)),

    // Разделы строятся из тех же данных, что и списки: счётчик и экран не расходятся.
    http.get("*/api/v1/spaces/:spaceId/sections", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const spaceId = String(params.spaceId);
      if (!hasSpace(spaceId)) return problem(404, "not_found", "Пространство не найдено");
      if (quiet || !seedSourceOf(db, spaceId)) return HttpResponse.json([]);
      const mine = <T extends { provenance: S["Provenance"] }>(rows: T[]) =>
        rows.filter((row) => row.provenance.spaceId === spaceId).length;
      const events = seedEvents(db).filter((e) => e.provenance.spaceId === spaceId);
      const rooms = new Set(events.map((e) => e.location).filter(Boolean));
      const deadlines = mine(seedDeadlines(db));
      const counts: [S["SectionOut"]["kind"], number][] = [
        ["courses", mine(seedCourses(db))],
        ["scheduleEvents", events.length],
        ["deadlines", deadlines],
        ["grades", mine(seedGrades(db))],
        ["people", mine(seedPeople(db))],
        ["locations", rooms.size],
        ["news", mine(seedNews(db))],
        ["documents", docs().filter((d) => d.spaceId === spaceId).length],
      ];
      return HttpResponse.json(
        counts.filter(([, count]) => count > 0).map(([kind, count]) => ({ kind, count })),
      );
    }),

    http.get("*/api/v1/spaces/:spaceId/documents", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const spaceId = String(params.spaceId);
      if (!hasSpace(spaceId)) return problem(404, "not_found", "Пространство не найдено");
      return HttpResponse.json(
        docs()
          .filter((d) => d.spaceId === spaceId)
          .sort((a, b) => b.fetchedAt.localeCompare(a.fetchedAt)),
      );
    }),

    http.post("*/api/v1/spaces/:spaceId/documents/url", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const spaceId = String(params.spaceId);
      if (!hasSpace(spaceId)) return problem(404, "not_found", "Пространство не найдено");
      const body = (await request.json()) as S["DocumentFromUrl"];
      let host = "";
      try {
        const parsed = new URL(body.url.trim());
        if (parsed.protocol !== "https:" && parsed.protocol !== "http:") throw new Error("scheme");
        host = parsed.hostname;
      } catch {
        return problem(422, "validation", "Нужна ссылка вида https://…");
      }
      const connection = addDocumentConnection(db, spaceId, host);
      const document: S["DocumentOut"] = {
        id: ulid(),
        spaceId,
        connectionId: connection.id,
        kind: "page",
        title: body.title?.trim() || host,
        url: body.url.trim(),
        mime: null,
        fetchedAt: new Date(db.now()).toISOString(),
      };
      const jobId = startJob({
        connectionId: connection.id,
        kind: "ingest",
        title: document.title,
        document,
      });
      save();
      return HttpResponse.json(
        { connectionId: connection.id, documentId: null, jobId } satisfies S["DocumentStarted"],
        { status: 201 },
      );
    }),

    http.post("*/api/v1/spaces/:spaceId/documents/upload", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const spaceId = String(params.spaceId);
      if (!hasSpace(spaceId)) return problem(404, "not_found", "Пространство не найдено");
      const form = await request.formData();
      const file = form.get("file");
      if (!(file instanceof File)) return problem(422, "validation", "Выберите файл");
      if (!ALLOWED_UPLOADS.test(file.name))
        return problem(
          422,
          "validation",
          "Этот тип файла не поддерживается. Подойдут PDF, DOCX, XLSX, CSV, HTML и текст",
        );
      if (file.size > MAX_UPLOAD_BYTES) return problem(422, "validation", "Файл больше 8 МБ");
      const connection = addDocumentConnection(db, spaceId, file.name);
      const document: S["DocumentOut"] = {
        id: ulid(),
        spaceId,
        connectionId: connection.id,
        kind: /\.(xlsx|csv)$/i.test(file.name) ? "sheet" : "file",
        title: file.name,
        url: null,
        mime: file.type || null,
        fetchedAt: new Date(db.now()).toISOString(),
      };
      const jobId = startJob({ connectionId: connection.id, kind: "ingest", title: file.name, document });
      save();
      return HttpResponse.json(
        { connectionId: connection.id, documentId: null, jobId } satisfies S["DocumentStarted"],
        { status: 201 },
      );
    }),

    http.delete("*/api/v1/documents/:documentId", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const id = String(params.documentId);
      const before = docs().length;
      documents = docs().filter((d) => d.id !== id);
      if (documents.length === before) return problem(404, "not_found", "Документ не найден");
      return new HttpResponse(null, { status: 204 });
    }),

    // Задание: каждый опрос продвигает его на шаг — читает, сохраняет, готово.
    http.get("*/api/v1/jobs/:jobId", async ({ params }) => {
      const job = jobs.get(String(params.jobId));
      if (!job) return; // чужое задание (подключение вуза) обрабатывает другой набор обработчиков
      await wait();
      if (!signedIn()) return unauthorized();
      job.polls += 1;
      const done = job.polls >= 3;
      if (done && job.document && !docs().some((d) => d.id === job.document?.id)) {
        docs().push(job.document);
        const connection = db.connections.find((c) => c.id === job.connectionId);
        if (connection) {
          connection.status = "active";
          connection.lastSyncAt = new Date(db.now()).toISOString();
        }
        save();
      }
      const body: S["JobOut"] = {
        id: job.id,
        kind: job.kind,
        connectionId: job.connectionId,
        state: done ? "done" : job.polls === 1 ? "queued" : "running",
        stage: done ? null : job.polls === 1 ? null : "Читаю документ",
        createdAt: job.createdAt,
        startedAt: job.polls > 1 ? job.createdAt : null,
        finishedAt: done ? new Date(db.now()).toISOString() : null,
        result: done && job.document ? { documentId: job.document.id } : null,
      };
      return HttpResponse.json(body);
    }),

    http.post("*/api/v1/connections/:connectionId/sync", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const connection = db.connections.find((c) => c.id === String(params.connectionId));
      if (!connection) return problem(404, "not_found", "Подключение не найдено");
      if (
        connection.status === "draft" ||
        connection.status === "learning" ||
        connection.status === "disabled"
      )
        return problem(409, "conflict", "Подключение ещё настраивается или отключено");
      const jobId = startJob({ connectionId: connection.id, kind: "sync", title: connection.title });
      return HttpResponse.json({ jobId } satisfies S["JobRef"], { status: 202 });
    }),

    // Секреты принимаются и не возвращаются: ни значения, ни его части в ответах нет.
    http.put("*/api/v1/connections/:connectionId/secrets", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const connection = db.connections.find((c) => c.id === String(params.connectionId));
      if (!connection) return problem(404, "not_found", "Подключение не найдено");
      const body = (await request.json()) as S["SecretIn"];
      const value = (body.value ?? "").trim();
      if (!value) return problem(422, "validation", "Значение не может быть пустым");
      if (body.kind === "ical_url" && !/^(https?|webcal):\/\//i.test(value))
        return problem(422, "validation", "Нужна ссылка на календарь (https:// или webcal://)");
      return new HttpResponse(null, { status: 204 });
    }),

    http.delete("*/api/v1/connections/:connectionId/password", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const connection = db.connections.find((c) => c.id === String(params.connectionId));
      if (!connection) return problem(404, "not_found", "Подключение не найдено");
      connection.hasPassword = false;
      save();
      return new HttpResponse(null, { status: 204 });
    }),

    http.delete("*/api/v1/connections/:connectionId", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const id = String(params.connectionId);
      if (!db.connections.some((c) => c.id === id))
        return problem(404, "not_found", "Подключение не найдено");
      db.connections = db.connections.filter((c) => c.id !== id);
      save();
      return new HttpResponse(null, { status: 204 });
    }),
  ];
}

/** Источник-документ: сервер заводит для него подключение вида `document`. */
function addDocumentConnection(db: MockDb, spaceId: string, title: string): S["ConnectionOut"] {
  const connection: S["ConnectionOut"] = {
    id: ulid(),
    spaceId,
    adapter: null,
    kind: "document",
    origin: "",
    title,
    status: "syncing",
    hasPassword: false,
    lastSyncAt: null,
    nextSyncAt: null,
    errorCode: null,
    errorDetail: null,
  };
  db.connections.push(connection);
  return connection;
}

import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import { fieldsHash } from "@/lib/hash";
import { createChatHandlers } from "./chat-handlers";
import { runConnect } from "./connect-script";
import { createDataHandlers } from "./data-handlers";
import {
  findUser,
  type MockConnection,
  type MockDb,
  type MockSpace,
  type MockUser,
  persistDb,
  ulid,
} from "./db";
import { JobSim } from "./job-sim";

type S = components["schemas"];

export interface HandlerOptions {
  /** Множитель пауз сценария подключения: 1 — как для человека, 0 — мгновенно (тесты). */
  jobPace?: number;
  /** Задержка ответов, мс: в браузере видны скелетоны, в тестах — 0. */
  latency?: number;
  /** Сохранять изменения между перезагрузками страницы (только в браузере). */
  persist?: boolean;
}

/** Ответ с `application/problem+json`, как у настоящего бэкенда. */
export function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const meOut = (db: MockDb, user: MockUser): S["MeOut"] => ({
  user: { id: user.id, email: user.email, displayName: user.displayName, isOwner: user.isOwner },
  quota: {
    tokensToday: 41_250,
    tokensLimit: 1_000_000,
    resetsAt: new Date(db.now() + 6 * 3_600_000).toISOString(),
  },
});

/**
 * Обработчики MSW по OpenAPI бэкенда. Один набор на браузер (`VITE_MOCKS=1`), Vitest и Playwright.
 * Эндпоинтов, которых нет в OpenAPI, здесь нет и не будет.
 */
type AskedBlock = {
  type?: string;
  mode?: string;
  required?: boolean;
  allowOther?: boolean;
  secret?: boolean;
  maxLength?: number;
  options?: { value: string }[];
};

/** Совпадает ли ответ по форме с вопросом. Повторяет проверки сервера: см. `_check_values` в `botai/jobs/answers.py`. */
export function answerFits(asked: unknown, values: S["AnswerIn"]["values"]): boolean {
  const block = (asked ?? {}) as AskedBlock;
  switch (block.type) {
    case "choice": {
      const picked = typeof values === "string" ? [values] : Array.isArray(values) ? values : null;
      if (!picked) return false;
      if (block.mode === "single" && picked.length !== 1) return false;
      if (block.required && picked.length === 0) return false;
      const allowed = new Set((block.options ?? []).map((option) => option.value));
      return picked.every(
        (value) =>
          allowed.has(value) || (block.allowOther === true && value.length > 0 && value.length <= 500),
      );
    }
    case "input":
      if (typeof values !== "string") return false;
      if ((block.required || block.secret) && values === "") return false;
      return values.length <= (block.maxLength ?? 200);
    case "confirm":
    case "approval":
      return typeof values === "boolean";
    case "login":
      return values === null || values === undefined || typeof values === "boolean";
    default:
      return false;
  }
}

type Proposal = {
  id: string;
  expiresAt: string;
  request: { fields: { name: string; value: string; editable: boolean }[] };
};

/** Блок `approval` с этим предложением среди заданий мока: так сервер находит предложение по id. */
function findApproval(db: MockDb, proposalId: string) {
  for (const sim of db.sims.values()) {
    for (const event of sim.events) {
      const data = event.data as { type?: string; id?: string; proposal?: Proposal };
      if (event.event === "block" && data.type === "approval" && data.proposal?.id === proposalId && data.id)
        return { sim, blockId: data.id, proposal: data.proposal };
    }
  }
  return null;
}

export function createHandlers(
  db: MockDb,
  { latency = 0, persist = false, jobPace = 0 }: HandlerOptions = {},
) {
  const save = () => {
    if (persist) persistDb(db);
  };
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const currentUser = () => findUser(db, db.sessionUserId);
  /** Защищённый ответ: без сессии — 401 с человеческим текстом. */
  const authed =
    <T>(build: (user: MockUser) => T | Response | Promise<T | Response>) =>
    async () => {
      await wait();
      const user = currentUser();
      if (!user) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const result = await build(user);
      return result instanceof Response ? result : HttpResponse.json(result as never);
    };

  return [
    ...createDataHandlers(db, { latency, persist, jobPace }),
    ...createChatHandlers(db, { latency, persist, jobPace }),
    http.post("*/api/v1/auth/login", async ({ request }) => {
      await wait();
      const body = (await request.json()) as S["LoginRequest"];
      const user = db.users.find((u) => u.email === body.email.trim().toLowerCase());
      if (!user || user.password !== body.password)
        return problem(401, "unauthorized", "Неверная почта или пароль");
      db.sessionUserId = user.id;
      save();
      return HttpResponse.json(meOut(db, user));
    }),

    http.post("*/api/v1/auth/register", async ({ request }) => {
      await wait();
      const body = (await request.json()) as S["RegisterRequest"];
      if (!db.invites.includes(body.inviteCode)) {
        return problem(403, "forbidden", "Приглашение недействительно, истекло или уже использовано");
      }
      const email = body.email.trim().toLowerCase();
      if (db.users.some((u) => u.email === email))
        return problem(409, "conflict", "Эта почта уже зарегистрирована");
      const user: MockUser = {
        id: ulid(),
        email,
        displayName: body.displayName,
        password: body.password,
        isOwner: false,
      };
      db.users.push(user);
      db.invites = db.invites.filter((code) => code !== body.inviteCode);
      db.sessionUserId = user.id;
      save();
      return HttpResponse.json(meOut(db, user), { status: 201 });
    }),

    http.post("*/api/v1/auth/logout", async () => {
      await wait();
      db.sessionUserId = null;
      save();
      return new HttpResponse(null, { status: 204 });
    }),

    http.get(
      "*/api/v1/me",
      authed((user) => meOut(db, user)),
    ),

    http.get("*/api/v1/universities", async ({ request }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const q = (new URL(request.url).searchParams.get("q") ?? "").trim().toLowerCase();
      const rows = q
        ? db.universities.filter((u) => `${u.title} ${u.shortTitle}`.toLowerCase().includes(q))
        : db.universities;
      return HttpResponse.json(rows);
    }),

    http.get(
      "*/api/v1/spaces",
      authed(() => db.spaces),
    ),

    http.post("*/api/v1/spaces", async ({ request }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const body = (await request.json()) as S["SpaceCreate"];
      const known = body.universityId ? db.universities.find((u) => u.id === body.universityId) : undefined;
      const created: MockSpace = {
        id: ulid(),
        title: body.title,
        shortTitle: known?.shortTitle ?? null,
        aliases: [],
        universityId: known?.id ?? null,
        accentHue: known?.accentHue ?? 325,
        timeZone: "Europe/Moscow",
        createdAt: new Date(db.now()).toISOString(),
      };
      db.spaces.push(created);
      save();
      return HttpResponse.json(created, { status: 201 });
    }),

    http.get("*/api/v1/spaces/:spaceId/connections", async ({ params }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const spaceId = String(params.spaceId);
      if (!db.spaces.some((s) => s.id === spaceId))
        return problem(404, "not_found", "Пространство не найдено");
      return HttpResponse.json(db.connections.filter((c) => c.spaceId === spaceId));
    }),

    http.get("*/api/v1/spaces/:spaceId/sections", async ({ params }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const spaceId = String(params.spaceId);
      if (!db.spaces.some((s) => s.id === spaceId))
        return problem(404, "not_found", "Пространство не найдено");
      const live = db.connections.some((c) => c.spaceId === spaceId && c.status !== "draft");
      const sections: S["SectionOut"][] = live
        ? [
            { kind: "courses", count: 6 },
            { kind: "grades", count: 14 },
            { kind: "people", count: 9 },
            { kind: "documents", count: 3 },
          ]
        : [];
      return HttpResponse.json(sections);
    }),

    http.post("*/api/v1/spaces/:spaceId/connections", async ({ params, request }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const space = db.spaces.find((item) => item.id === String(params.spaceId));
      if (!space) return problem(404, "not_found", "Пространство не найдено");
      const body = (await request.json()) as S["ConnectionCreate"];
      const university = db.universities.find((item) => item.id === space.universityId);
      const origin = body.url ?? university?.origins[0] ?? "https://lk.fakeuni.test";
      const connection: MockConnection = {
        id: ulid(),
        spaceId: space.id,
        adapter: null,
        kind: body.kind ?? "portal",
        origin,
        entryUrl: origin,
        threadId: null,
        activeTask: null,
        title: body.title ?? "Личный кабинет",
        status: "learning",
        hasPassword: false,
        lastSyncAt: null,
        nextSyncAt: null,
        errorCode: null,
        errorDetail: null,
      };
      db.connections.push(connection);
      const jobId = ulid();
      const sim = new JobSim(jobId, db.now, jobPace, connection.id, "connect");
      db.sims.set(jobId, sim);
      save();
      void runConnect(sim, db, connection.id, db.jobVariant).then(save);
      return HttpResponse.json({ connection, jobId } satisfies S["ConnectionStarted"], { status: 201 });
    }),

    http.get("*/api/v1/connections/:connectionId", async ({ params }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const connection = db.connections.find((item) => item.id === String(params.connectionId));
      return connection ? HttpResponse.json(connection) : problem(404, "not_found", "Подключение не найдено");
    }),

    http.get("*/api/v1/jobs/:jobId", async ({ params }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const sim = db.sims.get(String(params.jobId));
      if (!sim) return problem(404, "not_found", "Задание не найдено");
      const status = sim.events.findLast((event) => event.event === "status")?.data as
        | { stage?: string }
        | undefined;
      const done = sim.events.findLast((event) => event.event === "done")?.data as
        | { result?: Record<string, unknown> }
        | undefined;
      return HttpResponse.json({
        id: sim.jobId,
        kind: sim.kind,
        connectionId: sim.connectionId,
        state: sim.state,
        stage: status?.stage ?? null,
        createdAt: new Date(sim.startedAt || db.now()).toISOString(),
        startedAt: sim.startedAt ? new Date(sim.startedAt).toISOString() : null,
        finishedAt: sim.terminal ? new Date(db.now()).toISOString() : null,
        result: done?.result ?? null,
      } satisfies S["JobOut"]);
    }),

    http.get("*/api/v1/jobs/:jobId/events", async ({ params, request }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const sim = db.sims.get(String(params.jobId));
      if (!sim) return problem(404, "not_found", "Задание не найдено");
      const last = Number(request.headers.get("last-event-id") ?? 0) || 0;
      return new HttpResponse(sim.stream(last), {
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      });
    }),

    http.post("*/api/v1/jobs/:jobId/answers", async ({ params, request }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const sim = db.sims.get(String(params.jobId));
      if (!sim) return problem(404, "not_found", "Задание не найдено");
      const body = (await request.json()) as S["AnswerIn"];
      // Секретные поля сервер не повторяет и не журналирует: помечаем по самому блоку.
      const asked = sim.events.find(
        (event) => event.event === "block" && (event.data as { id?: string }).id === body.blockId,
      );
      if (!asked) return problem(404, "not_found", "Блок не найден");
      // Форма и размер — как у настоящего сервера (`botai/jobs/answers.py`); значение в ответе не повторяется.
      if (!answerFits(asked.data, body.values))
        return problem(422, "validation", "Ответ не подходит к вопросу");
      const secret = (asked.data as { secret?: boolean }).secret === true;
      const result = sim.answer(body.blockId, body.values, secret);
      if (result === "answered") return problem(409, "block_answered", "На этот вопрос уже ответили");
      if (!result) return problem(404, "not_found", "Вопрос не найден");
      return new HttpResponse(null, { status: 204 });
    }),

    http.post("*/api/v1/jobs/:jobId/cancel", async ({ params }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      db.sims.get(String(params.jobId))?.cancel();
      return new HttpResponse(null, { status: 204 });
    }),

    http.post("*/api/v1/actions/:proposalId/approve", async ({ params, request }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const found = findApproval(db, String(params.proposalId));
      if (!found) return problem(404, "not_found", "Предложение не найдено");
      const body = (await request.json()) as S["ApproveIn"];
      const { proposal } = found;
      if (Date.parse(proposal.expiresAt) <= db.now())
        return problem(409, "action_expired", "Время подтверждения истекло, запросите заново");
      // Как `approve` на сервере: хеш считается по тому, что человек видел, а итоговые поля — по правкам.
      const original = Object.fromEntries(proposal.request.fields.map((field) => [field.name, field.value]));
      if (body.fieldsHash !== (await fieldsHash(original)))
        return problem(409, "action_changed", "Содержимое изменилось, посмотрите его ещё раз");
      const editable = new Set(
        proposal.request.fields.filter((field) => field.editable).map((field) => field.name),
      );
      const sameKeys =
        Object.keys(body.fields).sort().join("\u0000") === Object.keys(original).sort().join("\u0000");
      const untouched = Object.keys(original).every(
        (name) => editable.has(name) || body.fields[name] === original[name],
      );
      if (!sameKeys || !untouched)
        return problem(409, "action_changed", "Поля не совпадают с показанными вам");
      found.sim.answer(found.blockId, true, false);
      return HttpResponse.json({}, { status: 202 });
    }),

    http.post("*/api/v1/actions/:proposalId/reject", async ({ params }) => {
      await wait();
      if (!currentUser()) return problem(401, "unauthorized", "Нужно войти в BotAI");
      const found = findApproval(db, String(params.proposalId));
      if (!found) return problem(404, "not_found", "Предложение не найдено");
      found.sim.answer(found.blockId, false, false);
      return new HttpResponse(null, { status: 204 });
    }),
  ];
}

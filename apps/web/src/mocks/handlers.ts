import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import { findUser, type MockDb, type MockUser, persistDb, ulid } from "./db";

type S = components["schemas"];

export interface HandlerOptions {
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
export function createHandlers(db: MockDb, { latency = 0, persist = false }: HandlerOptions = {}) {
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
      const created: S["SpaceOut"] = {
        id: ulid(),
        title: body.title,
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
  ];
}

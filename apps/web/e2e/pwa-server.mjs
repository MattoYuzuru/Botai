#!/usr/bin/env node
/**
 * Стенд для e2e PWA: отдаёт собранное приложение (`dist/`) и крошечный поддельный API с теми же формами ответов,
 * что у бэкенда. Нужен потому, что service worker существует только в сборке, а MSW (мок-режим) — только в dev:
 * два service worker на один scope не уживаются. Управление стендом — `/__test/*`:
 *   POST /__test/down      обрывать все запросы к API и данные (имитация пропавшей сети на стороне сервера);
 *   POST /__test/up        вернуть как было;
 *   POST /__test/new-sw    отдавать sw.js с новым содержимым (имитация выхода новой версии);
 *   POST /__test/keep-me   после выхода всё равно отвечать на /api/v1/me данными (имитация живой серверной сессии),
 *                          чтобы проверять стирание снимков именно страницей, а не ответом 401;
 *   POST /__test/reset     сбросить всё, включая выход из сессии.
 */
import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const port = Number(process.env.PWA_PORT ?? 5198);

const state = { down: false, newSw: false, signedIn: true, keepMe: false };

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
};

const SPACE = "01JMKCDB000000000000SPACE1";
const CONNECTION = "01JMKCDB00000000000CONN001";

function provenance(now) {
  return {
    connectionId: CONNECTION,
    spaceId: SPACE,
    sourceUrl: "https://lk.fakeuni.test/timetable",
    fetchedAt: new Date(now - 5 * 60_000).toISOString(),
    stale: false,
  };
}

function event(now) {
  // Текущий час остаётся в сегодняшнем дне Москвы даже после 23:00: фикстуру читают и /today, и расписание.
  const start = Math.floor(now / 3_600_000) * 3_600_000;
  return {
    id: "evt-1",
    title: "Алгоритмы и структуры данных",
    kind: "lecture",
    startsAt: new Date(start).toISOString(),
    endsAt: new Date(start + 80 * 60_000).toISOString(),
    format: "offline",
    location: "ауд. 412",
    teacher: "Сидорова Е. Н.",
    courseTitle: null,
    url: null,
    changed: false,
    conflictsWith: [],
    provenance: provenance(now),
  };
}

function deadline(now) {
  return {
    id: "dl-1",
    title: "Лабораторная: сортировки",
    dueAt: new Date(now + 48 * 3_600_000).toISOString(),
    effectiveDueAt: null,
    state: "open",
    courseTitle: "Алгоритмы",
    url: null,
    provenance: provenance(now),
  };
}

/** Ответы поддельного API по пути; `undefined` — такого адреса нет. */
function api(pathname) {
  const now = Date.now();
  if (pathname === "/api/v1/me") {
    return {
      user: {
        id: "01JMKCDB000000000000USER01",
        email: "student@example.test",
        displayName: "Анна",
        isOwner: false,
      },
      quota: {
        tokensToday: 0,
        tokensLimit: 1_000_000,
        resetsAt: new Date(now + 6 * 3_600_000).toISOString(),
      },
    };
  }
  if (pathname === "/api/v1/spaces") {
    return [
      {
        id: SPACE,
        title: "Высшая школа экономики",
        universityId: "u-hse",
        accentHue: 262.2,
        timeZone: "Europe/Moscow",
        createdAt: new Date(now - 40 * 86_400_000).toISOString(),
      },
    ];
  }
  if (pathname === "/api/v1/universities") {
    return [
      {
        id: "u-hse",
        title: "Высшая школа экономики",
        shortTitle: "ВШЭ",
        accentHue: 262.2,
        origins: ["https://lk.fakeuni.test"],
        thirdPartyWarning: null,
      },
    ];
  }
  if (pathname === `/api/v1/spaces/${SPACE}/connections`) {
    return [
      {
        id: CONNECTION,
        spaceId: SPACE,
        adapter: "recipe",
        kind: "portal",
        origin: "https://lk.fakeuni.test",
        title: "Личный кабинет",
        status: "active",
        hasPassword: true,
        lastSyncAt: new Date(now - 5 * 60_000).toISOString(),
        nextSyncAt: null,
        errorCode: null,
        errorDetail: null,
      },
    ];
  }
  if (pathname === `/api/v1/spaces/${SPACE}/sections`) return [{ kind: "courses", count: 1 }];
  if (pathname === "/api/v1/today") {
    return {
      date: new Date(now).toISOString().slice(0, 10),
      events: [event(now)],
      deadlines: [deadline(now)],
      attention: [],
    };
  }
  if (pathname === "/api/v1/schedule") return [event(now)];
  if (pathname === "/api/v1/deadlines") return [deadline(now)];
  return undefined;
}

function sendJson(response, status, body, extra = {}) {
  response.writeHead(status, { "content-type": "application/json", ...extra });
  response.end(JSON.stringify(body));
}

function serveFile(response, file, headers = {}) {
  const type = TYPES[extname(file)] ?? "application/octet-stream";
  response.writeHead(200, { "content-type": type, ...headers });
  createReadStream(file).pipe(response);
}

createServer((request, response) => {
  const url = new URL(request.url ?? "/", `http://127.0.0.1:${port}`);
  const { pathname } = url;

  if (pathname.startsWith("/__test/")) {
    if (pathname === "/__test/down") state.down = true;
    else if (pathname === "/__test/up") state.down = false;
    else if (pathname === "/__test/new-sw") state.newSw = true;
    else if (pathname === "/__test/keep-me") state.keepMe = true;
    else if (pathname === "/__test/reset")
      Object.assign(state, { down: false, newSw: false, signedIn: true, keepMe: false });
    return sendJson(response, 200, { ...state });
  }

  if (state.down) {
    // Обрыв соединения: у клиента это сетевая ошибка, как при пропавшей сети.
    request.socket.destroy();
    return;
  }

  if (pathname.startsWith("/api/")) {
    if (pathname === "/api/v1/auth/logout" && request.method === "POST") {
      state.signedIn = false;
      response.writeHead(204);
      return response.end();
    }
    if (!state.signedIn && !(state.keepMe && pathname === "/api/v1/me")) {
      return sendJson(
        response,
        401,
        {
          type: "x",
          title: "unauthorized",
          status: 401,
          code: "unauthorized",
          detail: "Нужно войти в BotAI",
        },
        { "content-type": "application/problem+json" },
      );
    }
    const body = api(pathname);
    if (body === undefined) return sendJson(response, 404, { code: "not_found", detail: "Не найдено" });
    return sendJson(response, 200, body);
  }

  if (pathname === "/sw.js") {
    const source = readFileSync(join(root, "sw.js"), "utf8");
    response.writeHead(200, { "content-type": TYPES[".js"], "cache-control": "no-cache" });
    return response.end(state.newSw ? `${source}\n// версия 2: ${Date.now()}\n` : source);
  }

  const candidate = normalize(join(root, pathname));
  if (
    pathname !== "/" &&
    candidate.startsWith(root) &&
    existsSync(candidate) &&
    statSync(candidate).isFile()
  ) {
    return serveFile(
      response,
      candidate,
      pathname.startsWith("/assets/") ? { "cache-control": "max-age=31536000, immutable" } : {},
    );
  }
  // Любой другой путь без расширения — страница приложения.
  serveFile(response, join(root, "index.html"), { "cache-control": "no-cache" });
}).listen(port, "127.0.0.1", () => {
  console.log(`pwa-стенд: http://127.0.0.1:${port}`);
});

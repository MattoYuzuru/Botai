/// <reference lib="webworker" />
/**
 * Service worker BotAI. Собирается плагином `botaiServiceWorker` (vite.config.ts) в `dist/sw.js`; список файлов
 * оболочки и идентификатор сборки подставляются при сборке. В dev-режиме не регистрируется: там работает MSW.
 * Логика — в `core.ts`, здесь только события.
 */
import {
  API_CACHE,
  type Deps,
  dropOldShells,
  networkFirstSnapshot,
  precache,
  routeOf,
  serveAsset,
  serveNavigation,
  trimSnapshots,
} from "./core";

declare const __PRECACHE__: readonly string[];
declare const __BUILD__: string;

const sw = self as unknown as ServiceWorkerGlobalScope;

function depsFor(event: ExtendableEvent): Deps {
  return {
    fetch: (request) => fetch(request),
    caches,
    now: () => Date.now(),
    waitUntil: (promise) => {
      try {
        event.waitUntil(promise);
      } catch {
        // Событие уже закончилось: запись в кэш не обязательна для ответа.
      }
    },
    setTimeout: (callback, ms) => setTimeout(callback, ms),
  };
}

// Новая версия ждёт: страница показывает «Доступна новая версия» и сама просит перейти (SKIP_WAITING).
sw.addEventListener("install", (event) => {
  event.waitUntil(precache(__PRECACHE__, __BUILD__, { caches, fetch: (request) => fetch(request) }));
});

sw.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      await dropOldShells(__BUILD__, { caches });
      await trimSnapshots(await caches.open(API_CACHE), Date.now());
      // Первая установка берёт страницу под контроль сразу: офлайн должен работать со следующего же открытия.
      await sw.clients.claim();
    })(),
  );
});

sw.addEventListener("message", (event) => {
  if ((event.data as { type?: string } | null)?.type === "SKIP_WAITING") void sw.skipWaiting();
});

sw.addEventListener("fetch", (event) => {
  const kind = routeOf(event.request, sw.location.origin);
  if (kind === "pass") return;
  const deps = depsFor(event);
  if (kind === "api") event.respondWith(networkFirstSnapshot(event.request, deps));
  else if (kind === "navigate") event.respondWith(serveNavigation(event.request, __BUILD__, deps));
  else event.respondWith(serveAsset(event.request, __BUILD__, deps));
});

/**
 * Ядро service worker: чистые функции с внедряемыми `fetch`, `caches` и часами — их проверяют тесты, а `sw.ts`
 * только подключает их к событиям. Файл собирается отдельно (tsconfig.sw.json, библиотека WebWorker), поэтому
 * здесь нет ни DOM, ни импортов приложения.
 *
 * Политика:
 *  - оболочка (`index.html`, ассеты сборки, иконки) — прекэш на каждую сборку, навигация отдаётся из него;
 *  - чтение данных (`/api/v1/today|schedule|deadlines|...`) — «сеть, затем снимок»: живой ответ кладётся в кэш,
 *    при сбое сети, ответе 5xx или молчании дольше 4 с, если снимок есть, отдаётся последний снимок с заголовком
 *    `x-botai-snapshot-at`; сеть при этом продолжает обновлять кэш в фоне (stale-while-revalidate по смыслу);
 *  - всё остальное (вход, задания, чат, действия, потоки) в обход — это не данные для чтения офлайн;
 *  - ответ 401 стирает снимки: чужому человеку на этом устройстве они не достанутся.
 */

export const API_CACHE = "botai-api-v1";
export const SHELL_PREFIX = "botai-shell-";
/** Заголовок, которым ответ из кэша помечается для страницы (читает features/data/fetch.ts). */
export const SNAPSHOT_HEADER = "x-botai-snapshot-at";
/** Когда живой ответ лёг в кэш: хранится вместе с ним. */
export const CACHED_HEADER = "x-botai-cached-at";
export const SNAPSHOT_AFTER_MS = 4000;
export const MAX_SNAPSHOTS = 80;
export const MAX_SNAPSHOT_AGE_MS = 30 * 24 * 3_600_000;

/** Что можно читать без сети: списки и справочники, не действия и не потоки. */
const SNAPSHOT_PATHS =
  /^\/api\/v1\/(me|usage|universities|today|schedule|deadlines|courses|grades|people|news|spaces|spaces\/[^/]+\/(connections|sections|documents)|connections\/[^/]+)$/;

export function isSnapshotPath(pathname: string): boolean {
  return SNAPSHOT_PATHS.test(pathname);
}

export type RouteKind = "api" | "navigate" | "asset" | "pass";

/** Как service worker обращается с запросом. */
export function routeOf(request: { method: string; mode: string; url: string }, origin: string): RouteKind {
  if (request.method !== "GET") return "pass";
  const url = new URL(request.url);
  if (url.origin !== origin) return "pass";
  if (url.pathname.startsWith("/api/")) return isSnapshotPath(url.pathname) ? "api" : "pass";
  if (url.pathname === "/sw.js" || url.pathname === "/mockServiceWorker.js") return "pass";
  return request.mode === "navigate" ? "navigate" : "asset";
}

export interface Deps {
  fetch: (request: Request) => Promise<Response>;
  caches: CacheStorage;
  now: () => number;
  /** Продлевает жизнь service worker до конца записи в кэш и фонового обновления. */
  waitUntil: (promise: Promise<unknown>) => void;
  setTimeout: (callback: () => void, ms: number) => unknown;
  snapshotAfterMs?: number;
}

/** Копия ответа с отметкой времени записи в кэш. */
function stamp(response: Response, nowMs: number): Response {
  const headers = new Headers(response.headers);
  headers.set(CACHED_HEADER, new Date(nowMs).toISOString());
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

/** Ответ из кэша, помеченный как снимок: страница узнаёт «это не живые данные» и когда они получены. */
export function asSnapshot(cached: Response, nowMs: number): Response {
  const headers = new Headers(cached.headers);
  headers.set(SNAPSHOT_HEADER, headers.get(CACHED_HEADER) ?? new Date(nowMs).toISOString());
  return new Response(cached.body, { status: cached.status, statusText: cached.statusText, headers });
}

/** Стирает самые старые и просроченные снимки сверх лимита. */
export async function trimSnapshots(cache: Cache, nowMs: number, max = MAX_SNAPSHOTS): Promise<void> {
  const requests = await cache.keys();
  const rows: { request: Request; at: number }[] = [];
  for (const request of requests) {
    const response = await cache.match(request);
    const stamped = response?.headers.get(CACHED_HEADER);
    rows.push({ request, at: stamped ? Date.parse(stamped) : 0 });
  }
  rows.sort((a, b) => b.at - a.at);
  await Promise.all(
    rows
      .filter((row, index) => index >= max || nowMs - row.at > MAX_SNAPSHOT_AGE_MS)
      .map((row) => cache.delete(row.request)),
  );
}

/**
 * «Сеть, затем снимок». Без снимка — просто сеть (ошибка сети доходит до страницы, и она ретраит в фоне).
 * Со снимком — снимок при сбое сети, ответе 5xx и молчании сети дольше `snapshotAfterMs`.
 */
export async function networkFirstSnapshot(request: Request, deps: Deps): Promise<Response> {
  // Проверка сессии перед исходящим действием должна получить живой ответ сервера.
  if (request.cache === "no-store") return deps.fetch(request);
  const cache = await deps.caches.open(API_CACHE);
  const live = deps.fetch(request).then((response) => {
    if (response.status === 401) {
      // Сессия закончилась: снимки прежнего человека не должны пережить это.
      deps.waitUntil(deps.caches.delete(API_CACHE));
    } else if (response.ok) {
      const writing = (async () => {
        await cache.put(request, stamp(response.clone(), deps.now()));
        if ((await cache.keys()).length > MAX_SNAPSHOTS + 10) await trimSnapshots(cache, deps.now());
      })().catch(() => undefined);
      deps.waitUntil(writing);
    }
    return response;
  });
  // Сбой до того, как кто-то дождётся ответа, не должен считаться необработанным.
  live.catch(() => undefined);

  let cached = await cache.match(request);
  if (cached && deps.now() - Date.parse(cached.headers.get(CACHED_HEADER) ?? "") > MAX_SNAPSHOT_AGE_MS) {
    // Снимок старше срока хранения: личные данные не показываем и не держим, даже если сессия ещё жива.
    deps.waitUntil(cache.delete(request));
    cached = undefined;
  }
  if (!cached) return live;

  const silent = new Promise<null>((resolve) =>
    deps.setTimeout(() => resolve(null), deps.snapshotAfterMs ?? SNAPSHOT_AFTER_MS),
  );
  try {
    const first = await Promise.race([live, silent]);
    if (first === null) {
      // Сеть молчит: показываем снимок, а запрос доживает и обновит кэш.
      deps.waitUntil(live.catch(() => undefined));
      return asSnapshot(cached, deps.now());
    }
    return first.status >= 500 ? asSnapshot(cached, deps.now()) : first;
  } catch {
    return asSnapshot(cached, deps.now());
  }
}

export const shellCacheName = (build: string): string => `${SHELL_PREFIX}${build}`;

/**
 * Кладёт оболочку в кэш этой сборки. Каждый файл берётся мимо HTTP-кэша браузера (`cache: "reload"`): иначе
 * свежая установка могла бы получить вчерашний `index.html`, который ссылается на уже удалённые ассеты.
 * Любая недоступная часть валит установку: оболочка должна быть целой.
 */
export async function precache(
  urls: readonly string[],
  build: string,
  deps: Pick<Deps, "caches" | "fetch"> & {
    /** Адрес, от которого считаются относительные пути: в service worker не нужен, в тестах — origin. */
    base?: string;
  },
): Promise<void> {
  const cache = await deps.caches.open(shellCacheName(build));
  await Promise.all(
    urls.map(async (url) => {
      const response = await deps.fetch(
        new Request(deps.base ? new URL(url, deps.base) : url, { cache: "reload" }),
      );
      if (!response.ok) throw new Error(`precache ${url}: ${response.status}`);
      await cache.put(url, response);
    }),
  );
}

/** Удаляет оболочки прошлых сборок; снимки данных не трогает. */
export async function dropOldShells(build: string, deps: Pick<Deps, "caches">): Promise<void> {
  const keep = shellCacheName(build);
  const names = await deps.caches.keys();
  await Promise.all(
    names
      .filter((name) => name.startsWith(SHELL_PREFIX) && name !== keep)
      .map((name) => deps.caches.delete(name)),
  );
}

/** Навигация: любая страница приложения — это `index.html` из прекэша (маршрутизирует клиент). */
export async function serveNavigation(
  request: Request,
  build: string,
  deps: Pick<Deps, "caches" | "fetch">,
): Promise<Response> {
  const shell = await (await deps.caches.open(shellCacheName(build))).match("/index.html");
  return shell ?? deps.fetch(request);
}

/** Ассеты сборки: из прекэша, иначе из сети. Хеш в имени файла делает кэш безопасным. */
export async function serveAsset(
  request: Request,
  build: string,
  deps: Pick<Deps, "caches" | "fetch">,
): Promise<Response> {
  const cached = await (await deps.caches.open(shellCacheName(build))).match(request, { ignoreSearch: true });
  return cached ?? deps.fetch(request);
}

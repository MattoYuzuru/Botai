import { describe, expect, it } from "vitest";
import {
  API_CACHE,
  CACHED_HEADER,
  type Deps,
  dropOldShells,
  isSnapshotPath,
  MAX_SNAPSHOTS,
  networkFirstSnapshot,
  precache,
  routeOf,
  SNAPSHOT_HEADER,
  serveAsset,
  serveNavigation,
  shellCacheName,
  trimSnapshots,
} from "./core";

/** Минимальный Cache API: хранит ответы как байты, чтобы их можно было читать повторно. */
class FakeCache {
  readonly rows = new Map<
    string,
    { status: number; statusText: string; headers: [string, string][]; body: ArrayBuffer }
  >();

  private key(request: Request | string) {
    return typeof request === "string" ? new URL(request, "https://botai.test").href : request.url;
  }

  async put(request: Request | string, response: Response) {
    this.rows.set(this.key(request), {
      status: response.status,
      statusText: response.statusText,
      headers: [...response.headers.entries()],
      body: await response.arrayBuffer(),
    });
  }

  async match(request: Request | string, options?: { ignoreSearch?: boolean }) {
    let key = this.key(request);
    if (options?.ignoreSearch) key = key.split("?")[0] as string;
    const row = this.rows.get(key) ?? (options?.ignoreSearch ? undefined : undefined);
    if (!row) return undefined;
    return new Response(row.body.slice(0), {
      status: row.status,
      statusText: row.statusText,
      headers: row.headers,
    });
  }

  async keys() {
    return [...this.rows.keys()].map((url) => new Request(url));
  }

  async delete(request: Request | string) {
    return this.rows.delete(this.key(request));
  }

  async addAll(urls: string[]) {
    for (const url of urls) await this.put(url, new Response(`file:${url}`));
  }
}

class FakeCaches {
  readonly stores = new Map<string, FakeCache>();
  async open(name: string) {
    let cache = this.stores.get(name);
    if (!cache) {
      cache = new FakeCache();
      this.stores.set(name, cache);
    }
    return cache as unknown as Cache;
  }
  async delete(name: string) {
    return this.stores.delete(name);
  }
  async keys() {
    return [...this.stores.keys()];
  }
  async match() {
    return undefined;
  }
  async has(name: string) {
    return this.stores.has(name);
  }
}

const ORIGIN = "https://botai.test";
const NOW = Date.UTC(2026, 9, 27, 14, 40);

function setup(
  fetchImpl: (request: Request) => Promise<Response>,
  options: { snapshotAfterMs?: number } = {},
) {
  const caches = new FakeCaches();
  const pending: Promise<unknown>[] = [];
  const timers: { callback: () => void; ms: number }[] = [];
  const deps: Deps = {
    fetch: fetchImpl,
    caches: caches as unknown as CacheStorage,
    now: () => NOW,
    waitUntil: (promise) => void pending.push(promise),
    setTimeout: (callback, ms) => void timers.push({ callback, ms }),
    ...options,
  };
  // Ждёт всё, что зарегистрировано через waitUntil, в том числе то, что появится по ходу ожидания
  // (запись в кэш регистрируется, когда приходит ответ сети, — уже после первого вызова).
  const settle = async () => {
    let seen = 0;
    while (seen < pending.length) {
      const batch = pending.slice(seen);
      seen = pending.length;
      await Promise.all(batch);
    }
  };
  return { caches, deps, pending, timers, settle };
}

const get = (path: string) => new Request(`${ORIGIN}${path}`);
const json = (body: unknown, init?: ResponseInit) =>
  new Response(JSON.stringify(body), { headers: { "content-type": "application/json" }, ...init });

describe("какие запросы service worker трогает", () => {
  const route = (path: string, extra: Partial<{ method: string; mode: string }> = {}) =>
    routeOf({ method: "GET", mode: "cors", url: `${ORIGIN}${path}`, ...extra }, ORIGIN);

  it("чтение данных — снимок; вход, задания, чат, действия и потоки — в обход", () => {
    for (const path of [
      "/api/v1/today",
      "/api/v1/schedule",
      "/api/v1/deadlines",
      "/api/v1/me",
      "/api/v1/spaces",
      "/api/v1/spaces/01J/connections",
      "/api/v1/spaces/01J/sections",
      "/api/v1/spaces/01J/documents",
      "/api/v1/courses",
      "/api/v1/universities",
    ]) {
      expect(route(path), path).toBe("api");
    }
    for (const path of [
      "/api/v1/auth/login",
      "/api/v1/jobs/01J/events",
      "/api/v1/jobs/01J",
      "/api/v1/chat/threads",
      "/api/v1/chat/threads/01J/messages",
      "/api/v1/actions/01J/approve",
      "/api/v1/connections/01J/login-session",
      "/api/health",
    ]) {
      expect(route(path), path).toBe("pass");
    }
    expect(isSnapshotPath("/api/v1/spaces/01J/connections")).toBe(true);
    expect(isSnapshotPath("/api/v1/spaces/01J/documents/upload")).toBe(false);
  });

  it("запись, чужой источник и сам sw.js не перехватываются; страницы — оболочка, файлы — ассеты", () => {
    expect(route("/api/v1/today", { method: "POST" })).toBe("pass");
    expect(routeOf({ method: "GET", mode: "cors", url: "https://cdn.example.test/x.js" }, ORIGIN)).toBe(
      "pass",
    );
    expect(route("/sw.js")).toBe("pass");
    expect(route("/mockServiceWorker.js")).toBe("pass");
    expect(route("/schedule", { mode: "navigate" })).toBe("navigate");
    expect(route("/assets/index-abc.js", { mode: "no-cors" })).toBe("asset");
    expect(route("/icons/icon-192.png")).toBe("asset");
  });
});

describe("«сеть, затем снимок»", () => {
  it("живой ответ идёт на страницу как есть и ложится в кэш с отметкой времени", async () => {
    const { deps, caches, settle } = setup(async () => json({ date: "2026-10-27" }));
    const response = await networkFirstSnapshot(get("/api/v1/today"), deps);
    expect(response.headers.get(SNAPSHOT_HEADER)).toBeNull();
    expect(await response.json()).toEqual({ date: "2026-10-27" });
    await settle();
    const cached = await (await caches.open(API_CACHE)).match(get("/api/v1/today"));
    expect(cached?.headers.get(CACHED_HEADER)).toBe(new Date(NOW).toISOString());
    expect(await cached?.json()).toEqual({ date: "2026-10-27" });
  });

  it("нет сети и есть снимок — отдаётся снимок с заголовком времени получения", async () => {
    let online = true;
    const { deps, settle } = setup(async () => {
      if (!online) throw new TypeError("Failed to fetch");
      return json({ items: [1, 2] });
    });
    await networkFirstSnapshot(get("/api/v1/deadlines?include_done=true"), deps);
    await settle();
    online = false;
    const response = await networkFirstSnapshot(get("/api/v1/deadlines?include_done=true"), deps);
    expect(response.status).toBe(200);
    expect(response.headers.get(SNAPSHOT_HEADER)).toBe(new Date(NOW).toISOString());
    expect(await response.json()).toEqual({ items: [1, 2] });
  });

  it("нет сети и снимка нет — ошибка сети доходит до страницы (она ретраит в фоне)", async () => {
    const { deps } = setup(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(networkFirstSnapshot(get("/api/v1/today"), deps)).rejects.toThrow("Failed to fetch");
  });

  it("сервер ответил 5xx, снимок есть — снимок; без снимка — ответ сервера", async () => {
    let status = 200;
    const { deps, settle } = setup(async () => json({ v: status }, { status }));
    await networkFirstSnapshot(get("/api/v1/news"), deps);
    await settle();
    status = 503;
    const snapshot = await networkFirstSnapshot(get("/api/v1/news"), deps);
    expect(snapshot.headers.get(SNAPSHOT_HEADER)).not.toBeNull();
    expect(await snapshot.json()).toEqual({ v: 200 });
    const fresh = setup(async () => json({ code: "boom" }, { status: 503 }));
    expect((await networkFirstSnapshot(get("/api/v1/news"), fresh.deps)).status).toBe(503);
  });

  it("ответы 4xx не кэшируются и не подменяются снимком: «не найдено» остаётся «не найдено»", async () => {
    let status = 200;
    const { deps, settle, caches } = setup(async () => json({ v: 1 }, { status }));
    await networkFirstSnapshot(get("/api/v1/courses"), deps);
    await settle();
    status = 404;
    const response = await networkFirstSnapshot(get("/api/v1/courses"), deps);
    expect(response.status).toBe(404);
    expect(response.headers.get(SNAPSHOT_HEADER)).toBeNull();
    // Прежний снимок цел: 404 не записан поверх него.
    const kept = await (await caches.open(API_CACHE)).match(get("/api/v1/courses"));
    expect(await kept?.json()).toEqual({ v: 1 });
  });

  it("снимок старше 30 суток не показывается и стирается: личные данные не лежат вечно", async () => {
    let online = true;
    const { deps, settle, caches } = setup(async () => {
      if (!online) throw new TypeError("Failed to fetch");
      return json({ v: 1 });
    });
    await networkFirstSnapshot(get("/api/v1/me"), deps);
    await settle();
    online = false;
    const later = { ...deps, now: () => NOW + 31 * 24 * 3_600_000 };
    await expect(networkFirstSnapshot(get("/api/v1/me"), later)).rejects.toThrow("Failed to fetch");
    await settle();
    expect(await (await caches.open(API_CACHE)).match(get("/api/v1/me"))).toBeUndefined();
  });

  it("сеть молчит дольше 4 с — снимок, а запрос доживает и обновляет кэш", async () => {
    let release: (response: Response) => void = () => {};
    let slow = false;
    const { deps, timers, settle, caches } = setup(() =>
      slow ? new Promise<Response>((resolve) => (release = resolve)) : Promise.resolve(json({ v: "old" })),
    );
    await networkFirstSnapshot(get("/api/v1/today"), deps);
    await settle();
    slow = true;
    const pending = networkFirstSnapshot(get("/api/v1/today"), deps);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(timers.map((timer) => timer.ms)).toEqual([4000]);
    timers[0]?.callback();
    const response = await pending;
    expect(response.headers.get(SNAPSHOT_HEADER)).not.toBeNull();
    expect(await response.json()).toEqual({ v: "old" });
    release(json({ v: "new" }));
    await settle();
    const updated = await (await caches.open(API_CACHE)).match(get("/api/v1/today"));
    expect(await updated?.json()).toEqual({ v: "new" });
  });

  it("401 стирает все снимки: данные прежнего человека не переживают конец сессии", async () => {
    let status = 200;
    const { deps, settle, caches } = setup(async () => json({ v: 1 }, { status }));
    await networkFirstSnapshot(get("/api/v1/today"), deps);
    await networkFirstSnapshot(get("/api/v1/me"), deps);
    await settle();
    expect(await caches.has(API_CACHE)).toBe(true);
    status = 401;
    const response = await networkFirstSnapshot(get("/api/v1/me"), deps);
    await settle();
    expect(response.status).toBe(401);
    expect(await caches.has(API_CACHE)).toBe(false);
  });

  it("лимит снимков: старые и просроченные стираются, свежие остаются", async () => {
    const { caches } = setup(async () => json({}));
    const cache = await caches.open(API_CACHE);
    const stamp = (at: number) =>
      new Response("{}", { headers: { [CACHED_HEADER]: new Date(at).toISOString() } });
    for (let index = 0; index < 5; index++) {
      await cache.put(get(`/api/v1/schedule?n=${index}`), stamp(NOW - index * 60_000));
    }
    await cache.put(get("/api/v1/schedule?old"), stamp(NOW - 40 * 24 * 3_600_000));
    await trimSnapshots(cache, NOW, 3);
    const left = (await cache.keys()).map((request) => new URL(request.url).search);
    expect(left.sort()).toEqual(["?n=0", "?n=1", "?n=2"]);
    expect(MAX_SNAPSHOTS).toBeGreaterThan(10);
  });
});

describe("оболочка", () => {
  const files = ["/index.html", "/assets/app-1.js", "/manifest.webmanifest"];

  it("прекэш кладёт оболочку в кэш сборки, навигация отдаёт index.html из него", async () => {
    const { deps, caches } = setup(async (request) => new Response(`file:${new URL(request.url).pathname}`));
    await precache(files, "b1", { ...deps, base: ORIGIN });
    expect(await caches.has(shellCacheName("b1"))).toBe(true);
    const page = await serveNavigation(
      new Request(`${ORIGIN}/schedule`, { headers: { accept: "text/html" } }),
      "b1",
      deps,
    );
    expect(await page.text()).toBe("file:/index.html");
  });

  it("без прекэша навигация идёт в сеть; ассеты — из кэша, иначе из сети", async () => {
    const { deps } = setup(async (request) =>
      request.cache === "reload"
        ? new Response(`file:${new URL(request.url).pathname}`)
        : new Response("network"),
    );
    expect(await (await serveNavigation(get("/tasks"), "none", deps)).text()).toBe("network");
    await precache(files, "b2", { ...deps, base: ORIGIN });
    expect(await (await serveAsset(get("/assets/app-1.js"), "b2", deps)).text()).toBe(
      "file:/assets/app-1.js",
    );
    expect(await (await serveAsset(get("/assets/other.js"), "b2", deps)).text()).toBe("network");
  });

  it("прекэш берёт файлы мимо HTTP-кэша браузера и не принимает неполную оболочку", async () => {
    const modes: string[] = [];
    const ok = setup(async (request) => {
      modes.push(request.cache);
      return new Response("x");
    });
    await precache(files, "b3", { ...ok.deps, base: ORIGIN });
    expect(modes).toEqual(["reload", "reload", "reload"]);
    const broken = setup(async (request) =>
      request.url.endsWith("app-1.js") ? new Response("gone", { status: 404 }) : new Response("x"),
    );
    await expect(precache(files, "b4", { ...broken.deps, base: ORIGIN })).rejects.toThrow("app-1.js: 404");
  });

  it("активация удаляет оболочки прошлых сборок, но не снимки данных", async () => {
    const { deps, caches } = setup(async () => new Response(""));
    await precache(files, "old", { ...deps, base: ORIGIN });
    await precache(files, "new", { ...deps, base: ORIGIN });
    await caches.open(API_CACHE);
    await dropOldShells("new", deps);
    expect((await caches.keys()).sort()).toEqual([API_CACHE, shellCacheName("new")].sort());
    expect(await caches.has(shellCacheName("old"))).toBe(false);
    expect(await caches.has(API_CACHE)).toBe(true);
  });
});

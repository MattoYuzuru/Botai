import { expect, type Page, test } from "@playwright/test";

/**
 * PWA на собранном приложении с настоящим service worker (стенд e2e/pwa-server.mjs, проект `pwa`).
 * Серверное состояние стенда общее, поэтому тесты идут по очереди и каждый начинает со сброса.
 */
test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  await page.request.post("/__test/reset");
});

const today = (page: Page) => page.locator("[data-screen=today]");
const snapshotKeys = (page: Page) =>
  page.evaluate(async () => {
    if (!(await caches.has("botai-api-v1"))) return [];
    const cache = await caches.open("botai-api-v1");
    return (await cache.keys()).map((request) => new URL(request.url).pathname);
  });

/** Первое открытие: service worker ставится и берёт страницу; после перезагрузки данные идут через него и ложатся в снимки. */
async function installAndWarm(page: Page) {
  await page.goto("/");
  await expect(today(page)).toHaveAttribute("data-state", "ok");
  await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
  await page.reload();
  await expect(today(page)).toHaveAttribute("data-state", "ok");
  await expect.poll(() => snapshotKeys(page)).toContain("/api/v1/today");
  // Соседние вкладки подогреваются сами: открывать их заранее не нужно.
  await expect
    .poll(() => snapshotKeys(page))
    .toEqual(expect.arrayContaining(["/api/v1/schedule", "/api/v1/deadlines"]));
}

test("манифест и иконки отдаются; приложение установимо: манифест, иконки 192 и 512, service worker", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute("href", "/manifest.webmanifest");
  const response = await page.request.get("/manifest.webmanifest");
  expect(response.ok()).toBe(true);
  expect(response.headers()["content-type"]).toContain("manifest+json");
  const manifest = await response.json();
  expect(manifest.name).toBe("BotAI");
  expect(manifest.display).toBe("standalone");
  for (const icon of manifest.icons as { src: string }[]) {
    expect((await page.request.get(icon.src)).ok(), icon.src).toBe(true);
  }
  const sizes = (manifest.icons as { sizes: string }[]).map((icon) => icon.sizes);
  expect(sizes).toEqual(expect.arrayContaining(["192x192", "512x512"]));
  await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
  const registration = await page.evaluate(async () => {
    const found = await navigator.serviceWorker.getRegistration();
    return { scope: found?.scope, active: found?.active?.state };
  });
  expect(registration.scope).toMatch(/\/$/);
  expect(registration.active).toBe("activated");
});

test("favicon декодируется, а портрет maskable непрозрачный и целиком внутри безопасного круга", async ({
  page,
}) => {
  await page.goto("/");
  const ico = await page.request.get("/favicon.ico");
  expect(ico.ok()).toBe(true);
  expect((await ico.body()).subarray(0, 6)).toEqual(Buffer.from([0, 0, 1, 0, 3, 0]));
  const icons = await page.evaluate(async () => {
    async function pixels(src: string, size: number) {
      const img = new Image();
      img.width = img.height = size;
      img.src = src;
      await img.decode();
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = size;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Canvas 2D недоступен");
      context.drawImage(img, 0, 0, size, size);
      return context.getImageData(0, 0, size, size).data;
    }
    // The responsive SVG and the ICO's small PNG counterpart must remain real,
    // readable images, including after a static-host fallback would return HTML.
    const small = await pixels("/favicon-varya-v2.svg", 16);
    const png = await pixels("/favicon-varya-v2-16.png", 16);
    const manifest = await (await fetch("/manifest.webmanifest")).json();
    const mask = manifest.icons.find((icon: { purpose?: string }) => icon.purpose === "maskable");
    const bitmap = await pixels(mask.src, 512);
    const background = bitmap.slice(0, 4);
    let ink = 0;
    let outside = 0;
    let transparent = 0;
    for (let y = 0; y < 512; y++) {
      for (let x = 0; x < 512; x++) {
        const offset = (y * 512 + x) * 4;
        if (bitmap[offset + 3] !== 255) transparent++;
        if (bitmap.slice(offset, offset + 4).every((channel, i) => channel === background[i])) continue;
        ink++;
        if (Math.hypot(x + 0.5 - 256, y + 0.5 - 256) > 512 * 0.4) outside++;
      }
    }
    return {
      tinyMatches: small.every((channel, index) => channel === png[index]),
      ink,
      outside,
      transparent,
    };
  });
  expect(icons.tinyMatches).toBe(true);
  expect(icons.ink).toBeGreaterThan(30_000);
  expect(icons.outside).toBe(0);
  expect(icons.transparent).toBe(0);
});

test("после первого открытия оболочка и снимки данных лежат в кэше", async ({ page }) => {
  await installAndWarm(page);
  const shell = await page.evaluate(async () => {
    const names = (await caches.keys()).filter((name) => name.startsWith("botai-shell-"));
    const cache = await caches.open(names[0] ?? "");
    return {
      names,
      index: Boolean(await cache.match("/index.html")),
      manifest: Boolean(await cache.match("/manifest.webmanifest")),
    };
  });
  expect(shell.names).toHaveLength(1);
  expect(shell.index).toBe(true);
  expect(shell.manifest).toBe(true);
  expect(await snapshotKeys(page)).toEqual(
    expect.arrayContaining(["/api/v1/me", "/api/v1/spaces", "/api/v1/today"]),
  );
});

test("нет сети: страница открывается из кэша и показывает последний снимок, а не пустой экран", async ({
  page,
  context,
}) => {
  await installAndWarm(page);
  await page.request.post("/__test/down");
  await context.setOffline(true);
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Сегодня" })).toBeVisible();
  await expect(today(page)).toHaveAttribute("data-state", "offline");
  await expect(page.getByText(/Нет сети\. Показан снимок от/).first()).toBeVisible();
  await expect(page.getByText("Алгоритмы и структуры данных").first()).toBeVisible();
  await expect(page.getByText("Лабораторная: сортировки")).toBeVisible();
  // Ни кнопок повтора, ни пустого экрана.
  await expect(page.getByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toHaveCount(0);
  // Другие вкладки открываются из той же оболочки.
  await page
    .getByRole("navigation", { name: "Основная навигация" })
    .getByRole("link", { name: "Расписание" })
    .click();
  await expect(page.getByRole("heading", { level: 1, name: "Расписание" })).toBeVisible();
  await expect(page.getByText("Алгоритмы и структуры данных").first()).toBeVisible();
  // Сеть вернулась — данные снова живые.
  await context.setOffline(false);
  await page.request.post("/__test/up");
  await page
    .getByRole("navigation", { name: "Основная навигация" })
    .getByRole("link", { name: "Сегодня" })
    .click();
  await expect(today(page)).toHaveAttribute("data-state", "ok", { timeout: 30_000 });
});

test("сервер недоступен, а сеть есть: снимок помечен устаревшим, обновление идёт само", async ({ page }) => {
  await installAndWarm(page);
  await page.request.post("/__test/down");
  await page.reload();
  await expect(today(page)).toHaveAttribute("data-state", "stale");
  await expect(page.getByText("Алгоритмы и структуры данных").first()).toBeVisible();
  const header = await page.evaluate(async () =>
    (await fetch("/api/v1/today")).headers.get("x-botai-snapshot-at"),
  );
  expect(header).toMatch(/^\d{4}-\d\d-\d\dT/);
  // Сервер вернулся: фоновое обновление само заменяет снимок живыми данными.
  await page.request.post("/__test/up");
  await expect(today(page)).toHaveAttribute("data-state", "ok", { timeout: 40_000 });
});

test("новая версия: плашка «Доступна новая версия», «Перезапустить» применяет её без потери страницы", async ({
  page,
}) => {
  await installAndWarm(page);
  await expect(page.getByText("Доступна новая версия")).toHaveCount(0);
  await page.request.post("/__test/new-sw");
  await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.getRegistration();
    await registration?.update();
  });
  const banner = page.getByText("Доступна новая версия");
  await expect(banner).toBeVisible({ timeout: 20_000 });
  const box = await page.locator("[data-pwa-update]").boundingBox();
  expect(box?.y ?? 0).toBeGreaterThan(300);
  await page.getByRole("button", { name: "Перезапустить" }).click();
  await expect(page.getByText("Доступна новая версия")).toBeHidden({ timeout: 20_000 });
  await expect(today(page)).toHaveAttribute("data-state", "ok");
});

test("новая версия на странице, открытой ещё при первой установке: «Перезапустить» тоже перезагружает её", async ({
  page,
}) => {
  // Без reload после установки: страница так и осталась той, что была открыта до появления service worker.
  await page.goto("/");
  await expect(today(page)).toHaveAttribute("data-state", "ok");
  await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
  await page.evaluate(() => {
    (window as unknown as { __sameDocument: boolean }).__sameDocument = true;
  });
  await page.request.post("/__test/new-sw");
  await page.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.update());
  await expect(page.getByText("Доступна новая версия")).toBeVisible({ timeout: 20_000 });
  // Перезагрузка — это навигация: ждём её вместе с нажатием.
  await Promise.all([
    page.waitForEvent("framenavigated", { timeout: 20_000 }),
    page.getByRole("button", { name: "Перезапустить" }).click(),
  ]);
  await expect(today(page)).toHaveAttribute("data-state", "ok");
  // Прежний документ (с нашей меткой) заменён новым.
  expect(
    await page.evaluate(() => (window as unknown as { __sameDocument?: boolean }).__sameDocument ?? false),
  ).toBe(false);
  await expect(page.getByText("Доступна новая версия")).toHaveCount(0);
});

test("выход стирает офлайн-снимки: следующий человек на устройстве их не увидит", async ({ page }) => {
  await installAndWarm(page);
  // Сервер после выхода продолжает отвечать на /me, как при живой серверной сессии: снимки должна стереть сама
  // страница, а не ответ 401 (его стирает service worker, и он замаскировал бы пропажу очистки на выходе).
  await page.request.post("/__test/keep-me");
  await page
    .getByRole("navigation", { name: "Основная навигация" })
    .getByRole("link", { name: "Учёба" })
    .click();
  await page.getByRole("button", { name: "Выйти из BotAI" }).click();
  await expect(page).toHaveURL(/\/login|\/$/);
  await expect
    .poll(() => page.evaluate(async () => caches.has("botai-api-v1")), { timeout: 15_000 })
    .toBe(false);
});

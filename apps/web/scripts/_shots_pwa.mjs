import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "@playwright/test";

const out = process.argv[2];
mkdirSync(out, { recursive: true });
const base = "http://127.0.0.1:5198";
const IPHONE =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";
const viewports = [
  { label: "390", width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  { label: "1280", width: 1280, height: 800, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
];

async function warm(page) {
  await page.goto(`${base}/`);
  await page.waitForSelector("[data-screen=today][data-state=ok]");
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await page.reload();
  await page.waitForSelector("[data-screen=today][data-state=ok]");
  await page.waitForFunction(async () => {
    if (!(await caches.has("botai-api-v1"))) return false;
    const keys = (await (await caches.open("botai-api-v1")).keys()).map((r) => new URL(r.url).pathname);
    return keys.includes("/api/v1/deadlines") && keys.includes("/api/v1/schedule");
  });
}

const states = {
  "offline-today": async (page, context) => {
    await warm(page);
    await page.request.post(`${base}/__test/down`);
    await context.setOffline(true);
    await page.reload();
    await page.waitForSelector("[data-screen=today][data-state=offline]");
  },
  "offline-schedule": async (page, context) => {
    await warm(page);
    await page.request.post(`${base}/__test/down`);
    await context.setOffline(true);
    await page.goto(`${base}/schedule`);
    await page.waitForSelector("[data-screen=schedule][data-state=offline]");
  },
  "update-banner": async (page) => {
    await warm(page);
    await page.request.post(`${base}/__test/new-sw`);
    await page.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.update());
    await page.getByText("Доступна новая версия").waitFor({ timeout: 20000 });
  },
};

const browser = await chromium.launch();
try {
  for (const viewport of viewports) {
    for (const theme of ["light", "dark"]) {
      for (const [name, run] of [
        ...Object.entries(states),
        [
          "ios-hint",
          async (page) => {
            await page.goto(`${base}/`);
            await page.waitForSelector("[data-screen=today][data-state=ok]");
            await page.getByText("Добавьте BotAI на экран «Домой»").waitFor();
          },
        ],
      ]) {
        await fetch(`${base}/__test/reset`, { method: "POST" });
        const context = await browser.newContext({
          viewport: { width: viewport.width, height: viewport.height },
          deviceScaleFactor: viewport.deviceScaleFactor,
          isMobile: viewport.isMobile,
          hasTouch: viewport.hasTouch,
          locale: "ru-RU",
          timezoneId: "Europe/Moscow",
          colorScheme: theme,
          reducedMotion: "reduce",
          ...(name === "ios-hint" ? { userAgent: IPHONE } : {}),
        });
        await context.addInitScript((value) => window.localStorage.setItem("botai.theme", value), theme);
        const page = await context.newPage();
        await run(page, context);
        await page.waitForTimeout(600);
        await page.screenshot({ path: join(out, `${name}-${viewport.label}-${theme}.png`) });
        await context.close();
      }
    }
  }
} finally {
  await fetch(`${base}/__test/reset`, { method: "POST" });
  await browser.close();
}
console.log("ok", out);

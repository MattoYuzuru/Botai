import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "@playwright/test";

const out = process.argv[2];
mkdirSync(out, { recursive: true });
const base = "http://127.0.0.1:5301";
const viewports = [
  { label: "390", width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  { label: "1280", width: 1280, height: 800, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
];

const states = {
  "connection-sheet": async (page) => {
    await page.goto(`${base}/study/connections?scenario=connected&latency=0`);
    await page.getByRole("button", { name: /Личный кабинет/ }).click();
  },
  "connection-secret": async (page) => {
    await page.goto(`${base}/study/connections?scenario=connected&latency=0`);
    await page.getByRole("button", { name: /Личный кабинет/ }).click();
    await page.getByRole("button", { name: /Добавить ссылку на календарь или ключ/ }).click();
    await page
      .getByRole("textbox", { name: "Ссылка на календарь" })
      .fill("https://my.fakeuni.test/calendar.ics?key=secret");
  },
  "connection-forget": async (page) => {
    await page.goto(`${base}/study/connections?scenario=connected&latency=0`);
    await page.getByRole("button", { name: /Личный кабинет/ }).click();
    await page.getByRole("button", { name: "Забыть пароль" }).click();
  },
  "document-form": async (page) => {
    await page.goto(`${base}/study/documents?scenario=connected&latency=0`);
    await page.getByRole("button", { name: "Добавить документ" }).click();
    await page.getByLabel("Ссылка").fill("https://docs.fakeuni.test/sheets/exams");
  },
  "document-file": async (page) => {
    await page.goto(`${base}/study/documents?scenario=connected&latency=0`);
    await page.getByRole("button", { name: "Добавить документ" }).click();
    await page.getByRole("radio", { name: "Файл" }).click();
    await page.locator("input[type=file]").setInputFiles({
      name: "Программа курса.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.alloc(340_000, 1),
    });
  },
  "document-progress": async (page) => {
    await page.goto(`${base}/study/documents?scenario=connected&latency=0`);
    await page.getByRole("button", { name: "Добавить документ" }).click();
    await page.getByLabel("Ссылка").fill("https://docs.fakeuni.test/sheets/exams");
    await page.getByLabel(/^Название/).fill("Расписание экзаменов");
    await page.getByRole("button", { name: "Добавить", exact: true }).click();
    await page.getByText("Читаю документ").waitFor({ timeout: 8000 });
  },
  "document-done": async (page) => {
    await page.goto(`${base}/study/documents?scenario=connected&latency=0`);
    await page.getByRole("button", { name: "Добавить документ" }).click();
    await page.getByLabel("Ссылка").fill("https://docs.fakeuni.test/sheets/exams");
    await page.getByLabel(/^Название/).fill("Расписание экзаменов");
    await page.getByRole("button", { name: "Добавить", exact: true }).click();
    await page.getByText(/Документ добавлен/).waitFor({ timeout: 15_000 });
  },
};

const browser = await chromium.launch();
try {
  for (const viewport of viewports) {
    for (const theme of ["light", "dark"]) {
      for (const [name, run] of Object.entries(states)) {
        const context = await browser.newContext({
          viewport: { width: viewport.width, height: viewport.height },
          deviceScaleFactor: viewport.deviceScaleFactor,
          isMobile: viewport.isMobile,
          hasTouch: viewport.hasTouch,
          locale: "ru-RU",
          timezoneId: "Europe/Moscow",
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await context.addInitScript((value) => window.localStorage.setItem("botai.theme", value), theme);
        const page = await context.newPage();
        await run(page);
        await page.waitForTimeout(500);
        await page.screenshot({ path: join(out, `${name}-${viewport.label}-${theme}.png`) });
        await context.close();
      }
    }
  }
} finally {
  await browser.close();
}
console.log("ok", out);

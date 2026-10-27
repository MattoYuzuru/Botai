#!/usr/bin/env node
/**
 * Снимки экранов для описания PR: 390 px (телефон) и 1280 px (ноутбук) в светлой и тёмной темах.
 *
 *   node scripts/capture.mjs --base http://127.0.0.1:5173 --out shots \
 *     --page "styleguide=/styleguide|palette,controls" --page today=/
 *
 * `--page имя=путь|якорь,якорь^кнопка`: без якорей снимается страница целиком, с якорями — каждый раздел
 * (элемент с этим id) отдельным файлом; `^кнопка` — нажать кнопку с таким именем перед снимком. Снимки — это витрина на синтетических данных, а не доказательство живого входа в вуз.
 */
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "@playwright/test";

const args = process.argv.slice(2);
const option = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 ? args[index + 1] : fallback;
};
const all = (name) => args.flatMap((arg, index) => (arg === `--${name}` ? [args[index + 1]] : []));

const base = option("base", "http://127.0.0.1:5173");
const out = option("out", "shots");
const pages = all("page").map((spec) => {
  const split = spec.indexOf("=");
  const name = spec.slice(0, split);
  const rest = spec.slice(split + 1) || "/";
  const [target, click = ""] = rest.split("^");
  const [path = "/", anchors = ""] = (target ?? "/").split("|");
  return { name, path, anchors: anchors ? anchors.split(",") : [], click };
});

const viewports = [
  { label: "390", width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  { label: "1280", width: 1280, height: 800, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
];
const themes = ["light", "dark"];

mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
try {
  for (const viewport of viewports) {
    for (const theme of themes) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        deviceScaleFactor: viewport.deviceScaleFactor,
        isMobile: viewport.isMobile,
        hasTouch: viewport.hasTouch,
        locale: "ru-RU",
        timezoneId: "Europe/Moscow",
        colorScheme: theme,
      });
      await context.addInitScript((value) => {
        window.localStorage.setItem("botai.theme", value);
      }, theme);
      for (const target of pages) {
        const page = await context.newPage();
        await page.goto(`${base}${target.path}`, { waitUntil: "domcontentloaded", timeout: 90_000 });
        await page.waitForSelector("h1", { timeout: 90_000 });
        await page.evaluate(() => document.fonts.ready);
        if (target.click) {
          // Действие перед снимком: нажать кнопку по имени (открыть шит, переключатель и т. п.).
          await page
            .getByRole("button", { name: new RegExp(target.click) })
            .first()
            .click();
          await page.waitForTimeout(500);
        }
        await page.waitForTimeout(400);
        const stem = `${target.name}-${viewport.label}-${theme}`;
        if (target.anchors.length === 0) {
          await page.screenshot({ path: join(out, `${stem}.png`), fullPage: true });
        } else {
          for (const anchor of target.anchors) {
            const locator = page.locator(`#${anchor}`).first();
            await locator.scrollIntoViewIfNeeded();
            await locator.screenshot({
              path: join(out, `${target.name}-${anchor}-${viewport.label}-${theme}.png`),
            });
          }
        }
        await page.close();
      }
      await context.close();
    }
  }
} finally {
  await browser.close();
}
console.log(`снимки сохранены в ${out}`);

#!/usr/bin/env node
/**
 * Иконки PWA из статичной Вари: `public/icons/varya.svg` → PNG 192, 512, maskable 512 и apple-touch 180.
 *
 *   node scripts/make-icons.mjs
 *
 * Происхождение `varya.svg`: `mascot/sprites/varya.svg` из репозитория doodle_vibe (коммит 9093261), копия без
 * правок; Botai и doodle_vibe — раздельные проекты, обратной синхронизации нет. PNG лежат в репозитории
 * (результат команды выше), в сборке ничего не растеризуется. Растеризует Chromium из Playwright — тот же, что
 * гоняет e2e, поэтому новых зависимостей нет.
 *
 * Фон — заливка `accent` (ВШЭ, ступень 600 шкалы: `#2a61ce`): у Вари белый контур, и на синем она читается и
 * на светлой, и на тёмной домашней странице. Для maskable (Android обрезает иконку маской) персонаж лежит
 * в безопасной зоне — центральные 60 %.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dir = join(root, "public", "icons");
mkdirSync(dir, { recursive: true });
const svg = readFileSync(join(dir, "varya.svg"), "utf8");
const BACKGROUND = "#2a61ce";

const targets = [
  { file: "icon-192.png", size: 192, scale: 0.78 },
  { file: "icon-512.png", size: 512, scale: 0.78 },
  { file: "icon-maskable-512.png", size: 512, scale: 0.6 },
  { file: "apple-touch-icon.png", size: 180, scale: 0.78 },
];

const browser = await chromium.launch();
try {
  for (const { file, size, scale } of targets) {
    const page = await browser.newPage({ viewport: { width: size, height: size }, deviceScaleFactor: 1 });
    const inner = Math.round(size * scale);
    await page.setContent(
      `<!doctype html><meta charset="utf-8"><style>
        html,body{margin:0;width:${size}px;height:${size}px;background:${BACKGROUND}}
        body{display:grid;place-items:center}
        svg{width:${inner}px;height:${inner}px;display:block}
      </style>${svg}`,
    );
    const png = await page.screenshot({ type: "png", omitBackground: false });
    writeFileSync(join(dir, file), png);
    await page.close();
    console.log(`${file} ${size}x${size}`);
  }
} finally {
  await browser.close();
}

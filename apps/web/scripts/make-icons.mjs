#!/usr/bin/env node
/**
 * Иконки BotAI: векторный портрет из renderer Вари →
 * favicon SVG/PNG/ICO, PNG 192/512, maskable 512, monochrome 512 и Apple touch 180.
 *
 *   node scripts/make-icons.mjs
 *
 * Палитра и анатомия едины с packages/design/src/mascot/vendor/varya.js.
 * На 16 px используется тот же крупный портрет без микродеталей. Версия в адресах нужна,
 * чтобы установленные PWA и браузеры не брали старую маленькую Варю из кэша.
 * Растеризует только закреплённый Playwright Chromium; новых зависимостей нет.
 * Иконка приложения v4 (192/512/maskable/Apple touch/SVG) — одна композиция на весь квадрат:
 * глубокий синий фон со светлым свечением за головой, голова Вари крупно, туловище уходит за
 * нижний край. Голова (клюв, гребень, глаза) целиком внутри центрального круга радиусом 40 %,
 * туловище — вне круга, как и положено «фону» maskable. Скруглений нет: их рисует ОС.
 * Монохромная иконка (Android themed) — белый силуэт головы с вырезанными глазами и зазором
 * вокруг клюва. Голову берём из вендоренного `varya.js` без правок его файла. Favicon остаются
 * v3: прозрачная крупная голова читается на 16 px. ICO содержит настоящие 32-bit DIB 16/32/48,
 * включая AND-маску: fallback работает и без поддержки SVG/PNG внутри ICO.
 * Результаты коммитятся; обычная сборка сайта не запускает Chromium.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { chromium } from "@playwright/test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const publicDir = join(root, "public");
const dir = join(publicDir, "icons");
mkdirSync(dir, { recursive: true });
// Портрет генерируется из той же анатомии, что и живая Варя в чате.
const sandbox = {};
vm.runInNewContext(
  readFileSync(join(root, "../../packages/design/src/mascot/vendor/varya.js"), "utf8"),
  sandbox,
);
const { PALETTE } = sandbox.Varya;
const faviconSvg = sandbox.Varya.portrait({ transparent: true });
writeFileSync(join(publicDir, "favicon-varya-v3.svg"), `${faviconSvg}\n`);

// Голова из прозрачного портрета: срезаем обёртку с его собственным преобразованием.
const wrapOpen = '<g transform="translate(-22 3) scale(1.4)">';
const wrapClose = "</g></g></g></svg>";
const headStart = faviconSvg.indexOf(wrapOpen);
if (headStart < 0 || !faviconSvg.endsWith(wrapClose)) throw new Error("формат Varya.portrait изменился");
const head = faviconSvg.slice(headStart + wrapOpen.length, faviconSvg.length - wrapClose.length);

// Голова занимает в координатах рисунка x 22–102, y 9–81, центр масс (64.4; 50.8).
// Масштаб и сдвиг подобраны по измерению пикселей: центр масс головы чуть ниже центра квадрата
// (гребень «тянет» вверх), самая дальняя точка (гребень, клюв) — не дальше 0.39 от стороны.
const HEAD_CENTER = [64.4, 50.8];
const placement = (scale, [x, y]) =>
  `translate(${(x - HEAD_CENTER[0] * scale).toFixed(2)} ${(y - HEAD_CENTER[1] * scale).toFixed(2)}) scale(${scale})`;
const BODY = "M30 66C14 88 4 120 0 160L120 160C114 120 108 88 94 66Z";

/** Цветная иконка приложения: фон на весь квадрат, голова целиком в безопасном круге. */
function appIconSvg() {
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128" role="img" aria-label="Варя — BotAI">` +
    `<defs><radialGradient id="varya-bg" cx="50%" cy="40%" r="72%">` +
    `<stop offset="0" stop-color="#9DBAF2"/><stop offset=".38" stop-color="#5E89DC"/>` +
    `<stop offset="1" stop-color="#193677"/></radialGradient></defs>` +
    `<rect width="128" height="128" fill="url(#varya-bg)"/>` +
    `<g transform="${placement(1.02, [64, 58])}"><path d="${BODY}" fill="${PALETTE.DARK}"/>${head}</g></svg>`
  );
}

/** Монохромная иконка: белая маска головы, глаза и зазор вокруг клюва вырезаны. */
function monochromeSvg() {
  const beaks = [...head.matchAll(/<g class="beak[LU]">[\s\S]*?<\/g>/g)].map((m) => m[0]);
  if (beaks.length !== 2) throw new Error("клюв не найден в Varya.portrait");
  const beakPaths = beaks.map((b) => /<path d="([^"]+)"/.exec(b)?.[1]);
  const face = head
    .replace(/<g class="beak[LU]">[\s\S]*?<\/g>/g, "")
    .replace(/<g class="detail">[\s\S]*?<\/g>/, "")
    .replace(/<path[^>]*opacity="\.65"\/>/, "")
    .replace(/fill="#FFF9E9"|fill="#FFFFFF"/g, 'fill="#000"')
    .replace(/fill="(?!#000")[^"]*"/g, 'fill="#fff"');
  const beak = beakPaths
    .map((d) => `<path d="${d}" fill="none" stroke="#000" stroke-width="2.8" stroke-linejoin="round"/>`)
    .concat(beakPaths.map((d) => `<path d="${d}" fill="#fff"/>`))
    .join("");
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128" role="img" aria-label="Варя — BotAI">` +
    `<defs><mask id="varya-mono" maskUnits="userSpaceOnUse" x="0" y="0" width="128" height="128">` +
    `<g transform="${placement(1.0, [64, 62])}">${face}${beak}</g></mask></defs>` +
    `<rect width="128" height="128" fill="#fff" mask="url(#varya-mono)"/></svg>`
  );
}

const appSvg = appIconSvg();
const monoSvg = monochromeSvg();
writeFileSync(join(dir, "varya-face-v4.svg"), `${appSvg}\n`);
writeFileSync(join(dir, "varya-monochrome-v4.svg"), `${monoSvg}\n`);

const targets = [
  { file: "favicon-varya-v3-16.png", size: 16, favicon: true },
  { file: "favicon-varya-v3-32.png", size: 32, favicon: true },
  { file: "favicon-varya-v3-48.png", size: 48, favicon: true },
  { file: "varya-v4-192.png", size: 192 },
  { file: "varya-v4-512.png", size: 512 },
  { file: "varya-v4-maskable-512.png", size: 512 },
  { file: "varya-v4-monochrome-512.png", size: 512, monochrome: true },
  { file: "varya-v4-apple-touch-180.png", size: 180 },
];

/** Windows ICO DIB: bottom-up BGRA plus DWORD-aligned 1-bit transparency mask. */
function dib(size, rgba) {
  const maskStride = Math.ceil(size / 32) * 4;
  const pixels = size * size * 4;
  const data = Buffer.alloc(40 + pixels + maskStride * size);
  data.writeUInt32LE(40, 0);
  data.writeInt32LE(size, 4);
  data.writeInt32LE(size * 2, 8);
  data.writeUInt16LE(1, 12);
  data.writeUInt16LE(32, 14);
  data.writeUInt32LE(pixels, 20);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const src = (y * size + x) * 4;
      const dst = 40 + ((size - y - 1) * size + x) * 4;
      data[dst] = rgba[src + 2];
      data[dst + 1] = rgba[src + 1];
      data[dst + 2] = rgba[src];
      data[dst + 3] = rgba[src + 3];
      if (rgba[src + 3] < 128) {
        data[40 + pixels + (size - y - 1) * maskStride + Math.floor(x / 8)] |= 0x80 >> (x % 8);
      }
    }
  }
  return data;
}

function ico(images) {
  const header = Buffer.alloc(6 + 16 * images.length);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(images.length, 4);
  let offset = header.length;
  for (const [index, { size, data }] of images.entries()) {
    const entry = 6 + index * 16;
    header[entry] = size;
    header[entry + 1] = size;
    header.writeUInt16LE(1, entry + 4);
    header.writeUInt16LE(32, entry + 6);
    header.writeUInt32LE(data.length, entry + 8);
    header.writeUInt32LE(offset, entry + 12);
    offset += data.length;
  }
  return Buffer.concat([header, ...images.map(({ data }) => data)]);
}

const browser = await chromium.launch();
const icoImages = [];
try {
  for (const target of targets) {
    const { file, size, monochrome, favicon } = target;
    const page = await browser.newPage({ viewport: { width: size, height: size }, deviceScaleFactor: 1 });
    const source = favicon ? faviconSvg : monochrome ? monoSvg : appSvg;
    // Один и тот же SVG растрируется в целевой размер без сторонних библиотек.
    await page.setContent(
      `<!doctype html><style>html,body{margin:0}img{display:block;width:${size}px;height:${size}px}</style>` +
        `<img alt="" src="data:image/svg+xml;base64,${Buffer.from(source).toString("base64")}">`,
    );
    const rgba = await page.evaluate(async () => {
      const img = document.querySelector("img");
      await img.decode();
      const canvas = document.createElement("canvas");
      canvas.width = innerWidth;
      canvas.height = innerHeight;
      const context = canvas.getContext("2d");
      context.drawImage(img, 0, 0, innerWidth, innerHeight);
      return Array.from(context.getImageData(0, 0, innerWidth, innerHeight).data);
    });
    const png = await page.screenshot({ type: "png", omitBackground: Boolean(favicon || monochrome) });
    writeFileSync(join(favicon ? publicDir : dir, file), png);
    if (favicon) icoImages.push({ size, data: dib(size, rgba) });
    await page.close();
    console.log(`${file} ${size}x${size}`);
  }
  writeFileSync(join(publicDir, "favicon.ico"), ico(icoImages));
  console.log("favicon.ico 16x16 + 32x32 + 48x48 (32-bit DIB)");
} finally {
  await browser.close();
}

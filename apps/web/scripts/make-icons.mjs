#!/usr/bin/env node
/**
 * Иконки BotAI: редактируемый пиксельный портрет `public/icons/varya-face-v2.svg` →
 * favicon SVG/PNG/ICO, PNG 192/512, maskable 512 и Apple touch 180.
 *
 *   node scripts/make-icons.mjs
 *
 * Палитра, происхождение Вари и отдельный рисунок 16 px описаны в исходном SVG.
 * Анимированный renderer в packages/design не меняется. Версия в адресах нужна,
 * чтобы установленные PWA и браузеры не брали старую маленькую Варю из кэша.
 * Растеризует только закреплённый Playwright Chromium; новых зависимостей нет.
 * Фон непрозрачный, maskable — без нарисованных скруглений, весь портрет внутри
 * центрального круга радиусом 40 %. ICO содержит настоящие 32-bit DIB 16/32/48,
 * включая AND-маску: fallback работает и без поддержки SVG/PNG внутри ICO.
 * Результаты коммитятся; обычная сборка сайта не запускает Chromium.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const publicDir = join(root, "public");
const dir = join(publicDir, "icons");
mkdirSync(dir, { recursive: true });
const svg = readFileSync(join(dir, "varya-face-v2.svg"), "utf8");
const faviconSvg = "favicon-varya-v2.svg";
// Detached SVG images (canvas, favicon consumers) may evaluate media queries at
// their intrinsic size. Default to the hand-tuned 16 px face even without layout;
// the canonical PWA portrait keeps its 32 px intrinsic viewport.
const faviconSource = svg.replace('width="32" height="32"', 'width="16" height="16"');
writeFileSync(join(publicDir, faviconSvg), faviconSource);

// The complete portrait, including the outermost feather, stays inside the
// platform's central 80 %-diameter safe circle after scaling about the centre.
const maskableSvg = svg.replace(
  '<g class="portrait">',
  '<g class="portrait" transform="translate(5.12 5.12) scale(0.68)">',
);

const targets = [
  { file: "favicon-varya-v2-16.png", size: 16, favicon: true },
  { file: "favicon-varya-v2-32.png", size: 32, favicon: true },
  { file: "favicon-varya-v2-48.png", size: 48, favicon: true },
  { file: "varya-v2-192.png", size: 192 },
  { file: "varya-v2-512.png", size: 512 },
  { file: "varya-v2-maskable-512.png", size: 512, maskable: true },
  { file: "varya-v2-apple-touch-180.png", size: 180 },
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
    const { file, size, maskable, favicon } = target;
    const page = await browser.newPage({ viewport: { width: size, height: size }, deviceScaleFactor: 1 });
    const source = maskable ? maskableSvg : svg;
    // An image viewport lets the SVG media query select the hand-tuned 16 px face.
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
    const png = await page.screenshot({ type: "png", omitBackground: false });
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

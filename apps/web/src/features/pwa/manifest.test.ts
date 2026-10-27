import { existsSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { parseOklch, toHex } from "@botai/design";
import { describe, expect, it } from "vitest";
import { buildId, precacheList } from "../../../vite-sw-plugin";

const WEB = join(import.meta.dirname, "../../..");
const TOKENS = join(WEB, "../../packages/design/src/styles/tokens.css");
const manifest = JSON.parse(readFileSync(join(WEB, "public/manifest.webmanifest"), "utf8")) as {
  id: string;
  name: string;
  short_name: string;
  lang: string;
  start_url: string;
  scope: string;
  display: string;
  background_color: string;
  theme_color: string;
  icons: { src: string; sizes: string; type: string; purpose?: string }[];
  shortcuts: { name: string; url: string }[];
};
const html = readFileSync(join(WEB, "index.html"), "utf8");

/** Цвет токена в hex — по формулам OKLab → sRGB, как в tokens.test.ts, а не по таблице в документе. */
function tokenHex(name: string): string {
  const css = readFileSync(TOKENS, "utf8");
  const match = new RegExp(`${name}:\\s*(oklch\\([^)]*\\))`).exec(css);
  const color = match?.[1] ? parseOklch(match[1]) : null;
  if (!color) throw new Error(`нет токена ${name}`);
  return toHex(color);
}

describe("манифест PWA", () => {
  it("установимый: имя BotAI, свой корень, standalone, русский", () => {
    expect(manifest.name).toBe("BotAI");
    expect(manifest.short_name).toBe("BotAI");
    expect(manifest.lang).toBe("ru");
    expect(manifest.display).toBe("standalone");
    expect(manifest.id).toBe("/");
    expect(manifest.scope).toBe("/");
    expect(manifest.start_url.startsWith(manifest.scope)).toBe(true);
  });

  it("цвета — токены: фон приложения светлой темы (`bg`), а не придуманный hex", () => {
    const bg = tokenHex("--palette-paper-50");
    expect(manifest.background_color).toBe(bg);
    expect(manifest.theme_color).toBe(bg);
    expect(html).toContain(`<meta name="theme-color" content="${bg}" media="(prefers-color-scheme: light)"`);
    expect(html).toContain(
      `<meta name="theme-color" content="${tokenHex("--palette-slate-925")}" media="(prefers-color-scheme: dark)"`,
    );
  });

  it("иконки: 192 и 512 PNG, отдельная maskable, векторная Варя; файлы существуют и не пустые", () => {
    const has = (size: string, purpose: string) =>
      manifest.icons.some(
        (icon) => icon.sizes === size && icon.type === "image/png" && icon.purpose === purpose,
      );
    expect(has("192x192", "any")).toBe(true);
    expect(has("512x512", "any")).toBe(true);
    expect(has("512x512", "maskable")).toBe(true);
    expect(manifest.icons.some((icon) => icon.type === "image/svg+xml")).toBe(true);
    for (const icon of manifest.icons) {
      const file = join(WEB, "public", icon.src);
      expect(existsSync(file), icon.src).toBe(true);
      expect(statSync(file).size, icon.src).toBeGreaterThan(500);
      if (icon.type === "image/png") {
        const bytes = readFileSync(file);
        expect(bytes.subarray(0, 8).toString("hex"), icon.src).toBe("89504e470d0a1a0a");
        expect(`${bytes.readUInt32BE(16)}x${bytes.readUInt32BE(20)}`, icon.src).toBe(icon.sizes);
      }
    }
  });

  it("ярлыки ведут на вкладки приложения", () => {
    expect(manifest.shortcuts.map((shortcut) => shortcut.url)).toEqual(["/", "/schedule", "/tasks"]);
  });

  it("index.html ссылается на манифест и иконки, в том числе для iOS", () => {
    expect(html).toContain('<link rel="manifest" href="/manifest.webmanifest"');
    expect(html).toContain('<link rel="apple-touch-icon" href="/icons/varya-v2-apple-touch-180.png"');
    expect(html).toContain('<meta name="apple-mobile-web-app-capable" content="yes"');
    const apple = readFileSync(join(WEB, "public/icons/varya-v2-apple-touch-180.png"));
    expect([apple.readUInt32BE(16), apple.readUInt32BE(20)]).toEqual([180, 180]);
    // Увеличение страницы не запрещено (styleguide §7).
    expect(html).not.toMatch(/user-scalable|maximum-scale/);
  });

  it("favicon имеет SVG и PNG, а настоящий корневой ICO содержит 16/32/48 px для браузеров без SVG", () => {
    expect(html).toContain('href="/favicon-varya-v2.svg" type="image/svg+xml" sizes="any"');
    expect(html).toContain('href="/favicon-varya-v2-32.png" type="image/png" sizes="32x32"');
    expect(html).toContain('href="/favicon.ico?v=2" type="image/x-icon"');
    const png = readFileSync(join(WEB, "public/favicon-varya-v2-32.png"));
    expect([png.readUInt32BE(16), png.readUInt32BE(20)]).toEqual([32, 32]);
    const bytes = readFileSync(join(WEB, "public/favicon.ico"));
    expect([bytes.readUInt16LE(0), bytes.readUInt16LE(2), bytes.readUInt16LE(4)]).toEqual([0, 1, 3]);
    for (const [index, size] of [16, 32, 48].entries()) {
      const entry = 6 + index * 16;
      expect([bytes[entry], bytes[entry + 1], bytes.readUInt16LE(entry + 6)]).toEqual([size, size, 32]);
      const offset = bytes.readUInt32LE(entry + 12);
      const length = bytes.readUInt32LE(entry + 8);
      expect(offset + length).toBeLessThanOrEqual(bytes.length);
      // ICO stores each bitmap bottom-up with doubled height (colour + AND mask).
      expect([bytes.readInt32LE(offset + 4), bytes.readInt32LE(offset + 8)]).toEqual([size, size * 2]);
    }
  });
});

describe("состав оболочки service worker", () => {
  it("список — файлы dist без карт и самого sw.js, index.html на месте", () => {
    const urls = precacheList([
      "index.html",
      "assets/index-abc.js",
      "assets/index-abc.js.map",
      "assets/app-1.css",
      "sw.js",
      "manifest.webmanifest",
      "icons/icon-192.png",
    ]);
    expect(urls).toEqual([
      "/assets/app-1.css",
      "/assets/index-abc.js",
      "/icons/icon-192.png",
      "/index.html",
      "/manifest.webmanifest",
    ]);
  });

  it("идентификатор сборки стабилен для одного состава и меняется вместе с хешем файла", () => {
    const first = precacheList(["index.html", "assets/a-1.js"]);
    expect(buildId(first)).toBe(buildId([...first]));
    expect(buildId(precacheList(["index.html", "assets/a-2.js"]))).not.toBe(buildId(first));
    expect(buildId(first)).toMatch(/^[0-9a-f]{12}$/);
  });

  it("правка постоянного по имени файла (страница, манифест, иконка) меняет идентификатор: браузеры увидят новый sw.js", () => {
    const urls = precacheList(["index.html", "assets/a-1.js", "manifest.webmanifest"]);
    const read = (page: string) => (url: string) => (url === "/index.html" ? page : "постоянное");
    expect(buildId(urls, read("<html>v1"))).toBe(buildId(urls, read("<html>v1")));
    expect(buildId(urls, read("<html>v2"))).not.toBe(buildId(urls, read("<html>v1")));
  });
});

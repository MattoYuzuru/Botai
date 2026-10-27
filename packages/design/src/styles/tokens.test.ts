/**
 * Проверка токенов по формулам OKLab → sRGB и WCAG 2.x, а не по таблице из стайлгайда:
 * гамут каждой ступени, контрасты пар §2 в обеих темах, акцент каждого вуза, полнота тёмной темы,
 * контракт имён и общее правило сокращённого движения. Значения читаются из tokens.css —
 * тест не даёт им разойтись с документом.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  ACCENT_HUE_WINDOW,
  accentScale,
  accentVariables,
  isAccentHueAllowed,
  LADDER_LIGHTNESS,
  type LadderStep,
  selectAccent,
  UNIVERSITY_ACCENTS,
} from "../lib/accents";
import {
  contrastRatio,
  formatOklch,
  isInSrgbGamut,
  type Oklch,
  parseHex,
  parseOklch,
  relativeLuminance,
  toHex,
  toRgb,
  WCAG_AA_NON_TEXT,
  WCAG_AA_TEXT,
} from "../lib/color";
import { THEME_COLORS } from "../lib/preferences";

const here = import.meta.dirname;
const tokensCss = readFileSync(resolve(here, "tokens.css"), "utf8");
const baseCss = readFileSync(resolve(here, "base.css"), "utf8");

type Declarations = Record<string, string>;

/** Делит строку по запятым верхнего уровня (внутри скобок запятые не считаются). */
function splitTopLevel(value: string, separator: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const char of value) {
    if (char === "(") depth++;
    if (char === ")") depth--;
    if (char === separator && depth === 0) {
      parts.push(current.trim());
      current = "";
    } else current += char;
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

/** Правила верхнего уровня: селектор → объявления. Вложенные блоки (@theme, @media) пропускаются. */
function topLevelRules(css: string): { selector: string; declarations: Declarations }[] {
  const source = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules: { selector: string; declarations: Declarations }[] = [];
  let index = 0;
  while (index < source.length) {
    const open = source.indexOf("{", index);
    if (open < 0) break;
    const selector = source.slice(index, open).replace(/\s+/g, " ").trim();
    let depth = 1;
    let cursor = open + 1;
    while (depth > 0 && cursor < source.length) {
      if (source[cursor] === "{") depth++;
      if (source[cursor] === "}") depth--;
      cursor++;
    }
    const body = source.slice(open + 1, cursor - 1);
    index = cursor;
    if (selector.startsWith("@")) continue;
    const declarations: Declarations = {};
    for (const statement of splitTopLevel(body, ";")) {
      const colon = statement.indexOf(":");
      if (colon < 0) continue;
      declarations[statement.slice(0, colon).trim()] = statement
        .slice(colon + 1)
        .replace(/\s+/g, " ")
        .trim();
    }
    rules.push({ selector, declarations });
  }
  return rules;
}

const rules = topLevelRules(tokensCss);
const rootRules = rules.filter((rule) => rule.selector === ":root");
const palette: Declarations = Object.assign({}, ...rootRules.map((rule) => rule.declarations));
const semantic = Object.fromEntries(Object.entries(palette).filter(([name]) => name.startsWith("--color-")));

type Theme = "light" | "dark";

/** Значение токена для темы → OKLCH. Понимает var(), oklch(), light-dark() и color-mix(… N%, transparent). */
function resolveValue(value: string, theme: Theme, vars: Declarations = palette): Oklch {
  const v = value.trim();
  const variable = /^var\((--[\w-]+)\)$/.exec(v);
  if (variable) {
    const next = vars[variable[1] as string];
    if (!next) throw new Error(`Нет переменной ${variable[1]}`);
    return resolveValue(next, theme, vars);
  }
  const lightDark = /^light-dark\((.*)\)$/.exec(v);
  if (lightDark) {
    const [light, dark] = splitTopLevel(lightDark[1] as string, ",");
    return resolveValue((theme === "light" ? light : dark) as string, theme, vars);
  }
  const mix = /^color-mix\(in oklab,\s*(.+?)\s+([\d.]+)%,\s*transparent\)$/.exec(v);
  if (mix) {
    const base = resolveValue(mix[1] as string, theme, vars);
    return { ...base, alpha: Number(mix[2]) / 100 };
  }
  const color = parseOklch(v);
  if (!color) throw new Error(`Не разобран цвет: ${v}`);
  return color;
}

const token = (theme: Theme, name: string, vars: Declarations = semantic): Oklch => {
  const value = vars[`--color-${name}`];
  if (!value) throw new Error(`Нет токена --color-${name}`);
  return resolveValue(value, theme, palette);
};

const THEMES: Theme[] = ["light", "dark"];
const SURFACES = ["bg", "bg-subtle", "surface", "surface-raised"];
const TEXT = ["fg", "fg-muted", "fg-subtle", "accent-fg", "info-fg", "success-fg", "warning-fg", "danger-fg"];
const SUBTLE_PAIRS = [
  ["accent-fg", "accent-subtle"],
  ["info-fg", "info-subtle"],
  ["success-fg", "success-subtle"],
  ["warning-fg", "warning-subtle"],
  ["danger-fg", "danger-subtle"],
] as const;
const GRAPHICS = ["info", "success", "warning", "control", "focus-ring"];

/** Имена из контракта между потоками работ: их нельзя терять и переименовывать. */
const CONTRACT = [
  "bg",
  "bg-subtle",
  "surface",
  "surface-raised",
  "overlay",
  "hover",
  "pressed",
  "border",
  "border-strong",
  "control",
  "fg",
  "fg-muted",
  "fg-subtle",
  "fg-on-accent",
  "accent",
  "accent-hover",
  "accent-subtle",
  "accent-fg",
  "info",
  "info-subtle",
  "info-fg",
  "success",
  "success-subtle",
  "success-fg",
  "warning",
  "warning-subtle",
  "warning-fg",
  "danger",
  "danger-hover",
  "danger-subtle",
  "danger-fg",
  "focus-ring",
];

describe("палитра", () => {
  const steps = Object.entries(palette).filter(([name]) => name.startsWith("--palette-"));

  it("содержит шкалы стайлгайда", () => {
    for (const scale of ["paper", "slate", "hse", "azure", "pine", "amber", "cranberry"]) {
      expect(
        steps.some(([name]) => name.startsWith(`--palette-${scale}-`)),
        scale,
      ).toBe(true);
    }
  });

  // Стайлгайд обещает C ≤ 0.985 от предела гаммы, но семь тёмных ступеней (azure-100/900/950,
  // pine-900/950, amber-800/900/950) выходят за неё на ≤ 0.001 линейной яркости: браузер обрезает
  // канал до того же hex, что в таблице (разница ≤ 2 кодов). Значения оставлены как в контракте.
  const GAMUT_TOLERANCE = 0.0015;

  it("каждая ступень лежит в гамуте sRGB (допуск 0.0015 линейной яркости, см. комментарий)", () => {
    const outside = steps
      .filter(([, value]) => {
        const color = parseOklch(value);
        return !color || !isInSrgbGamut(color, GAMUT_TOLERANCE);
      })
      .map(([name]) => name);
    expect(outside).toEqual([]);
  });

  it("ступени цветных шкал держат общую лестницу светлоты", () => {
    for (const scale of ["hse", "azure", "pine", "amber", "cranberry"]) {
      for (const [step, lightness] of Object.entries(LADDER_LIGHTNESS)) {
        const color = parseOklch(palette[`--palette-${scale}-${step}`] ?? "");
        expect(color?.l, `${scale}-${step}`).toBe(lightness);
      }
    }
  });

  it("ступень 900 шкалы ВШЭ — фирменный синий #0f2d69", () => {
    expect(toHex(resolveValue("var(--palette-hse-900)", "light"))).toBe("#0f2d69");
  });

  // Таблица hex из стайлгайда §2. Допуск — два кода канала: стайлгайд округлял иначе на тёмном краю шкал.
  const DOC_HEX: Record<string, string> = {
    "paper-0": "#ffffff",
    "paper-50": "#f9fafc",
    "paper-100": "#f4f5f8",
    "paper-150": "#eef0f3",
    "paper-200": "#e6e8eb",
    "paper-300": "#d4d6da",
    "slate-300": "#b2b6be",
    "slate-400": "#8a8e95",
    "slate-500": "#6c7077",
    "slate-600": "#565a60",
    "slate-700": "#3d4045",
    "slate-750": "#33353b",
    "slate-800": "#25282d",
    "slate-850": "#1c1e23",
    "slate-900": "#14161b",
    "slate-925": "#0e1115",
    "slate-950": "#090b0f",
    "hse-50": "#f1f6ff",
    "hse-100": "#e4edff",
    "hse-200": "#cbddfe",
    "hse-300": "#a8c6fe",
    "hse-400": "#7aa8fe",
    "hse-500": "#4c84f1",
    "hse-600": "#2a61ce",
    "hse-700": "#1e4fb1",
    "hse-800": "#183e8a",
    "hse-900": "#0f2d69",
    "hse-950": "#0c1f45",
    "azure-50": "#ebf8ff",
    "azure-100": "#d7f2ff",
    "azure-200": "#b0e6ff",
    "azure-300": "#6fd4ff",
    "azure-400": "#0bb9ef",
    "azure-500": "#0b96c2",
    "azure-600": "#027396",
    "azure-700": "#025f7d",
    "azure-800": "#004b63",
    "azure-900": "#01374a",
    "azure-950": "#012634",
    "pine-50": "#edfaf1",
    "pine-100": "#d8f5e2",
    "pine-200": "#b6ebc9",
    "pine-300": "#86dca7",
    "pine-400": "#45c580",
    "pine-500": "#09a361",
    "pine-600": "#047d49",
    "pine-700": "#01683b",
    "pine-800": "#01522e",
    "pine-900": "#023c21",
    "pine-950": "#012a15",
    "amber-50": "#fcf5e9",
    "amber-100": "#fbebcf",
    "amber-200": "#f6d8a7",
    "amber-300": "#ecbe6d",
    "amber-400": "#da9d0d",
    "amber-500": "#b27f07",
    "amber-600": "#896102",
    "amber-700": "#725001",
    "amber-800": "#5a3e01",
    "amber-900": "#422d01",
    "amber-950": "#2e1e01",
    "cranberry-50": "#fff3f1",
    "cranberry-100": "#ffe6e4",
    "cranberry-200": "#ffcfcb",
    "cranberry-300": "#feaea6",
    "cranberry-400": "#f98078",
    "cranberry-500": "#df5450",
    "cranberry-600": "#ba2b2e",
    "cranberry-700": "#9e1c22",
    "cranberry-800": "#7c181b",
    "cranberry-900": "#5e0f11",
    "cranberry-950": "#3e0f0e",
  };

  it.each(Object.entries(DOC_HEX))("ступень %s совпадает с hex стайлгайда (%s)", (name, hex) => {
    const got = parseHex(toHex(parseOklch(palette[`--palette-${name}`] ?? "") as Oklch));
    const want = parseHex(hex);
    for (const channel of ["r", "g", "b"] as const) {
      expect(Math.abs(got[channel] - want[channel]) * 255, `${name} ${channel}`).toBeLessThanOrEqual(2.01);
    }
  });
});

describe.each(THEMES)("тема: %s", (theme) => {
  const t = (name: string) => token(theme, name);

  it.each(TEXT.flatMap((fg) => SURFACES.map((bg) => [fg, bg] as const)))(
    "текст %s на %s — не меньше 4.5:1",
    (fg, bg) => {
      expect(contrastRatio(t(fg), t(bg))).toBeGreaterThanOrEqual(WCAG_AA_TEXT);
    },
  );

  it.each(SUBTLE_PAIRS)("текст %s на своей подложке %s — 6.4:1 и выше (минимум набора)", (fg, bg) => {
    expect(Math.round(contrastRatio(t(fg), t(bg)) * 10) / 10).toBeGreaterThanOrEqual(6.4);
  });

  it.each(["accent", "accent-hover", "danger", "danger-hover"])(
    "белый текст на заливке %s — не меньше 4.5:1",
    (fill) => {
      expect(contrastRatio(t("fg-on-accent"), t(fill))).toBeGreaterThanOrEqual(WCAG_AA_TEXT);
    },
  );

  it.each(GRAPHICS.flatMap((graphic) => ["bg", "surface"].map((base) => [graphic, base] as const)))(
    "графика %s на %s — не меньше 3:1",
    (graphic, base) => {
      expect(contrastRatio(t(graphic), t(base))).toBeGreaterThanOrEqual(WCAG_AA_NON_TEXT);
    },
  );

  it("заливка warning: метка срочной работы читается — bg-subtle на warning не меньше 4.5:1", () => {
    expect(contrastRatio(t("bg-subtle"), t("warning"))).toBeGreaterThanOrEqual(WCAG_AA_TEXT);
  });

  it("цвет строки состояния браузера (meta theme-color) равен токену bg", () => {
    expect(toHex(t("bg"))).toBe(THEME_COLORS[theme]);
  });

  it("граница выбранного контрола — control — читается на поверхностях и подложке поля", () => {
    for (const base of ["bg", "surface", "bg-subtle"]) {
      expect(contrastRatio(t("control"), t(base)), base).toBeGreaterThanOrEqual(WCAG_AA_NON_TEXT);
    }
  });

  it("рамки border и border-strong декоративны: они тише control", () => {
    expect(contrastRatio(t("border-strong"), t("bg"))).toBeLessThan(contrastRatio(t("control"), t("bg")));
    expect(contrastRatio(t("border"), t("bg"))).toBeLessThan(contrastRatio(t("border-strong"), t("bg")));
  });

  it("поверхности светлеют вместе со слоем", () => {
    const luminance = (name: string) => relativeLuminance(toRgb(t(name)));
    if (theme === "dark") {
      expect(luminance("bg-subtle")).toBeLessThan(luminance("bg"));
      expect(luminance("bg")).toBeLessThan(luminance("surface"));
      expect(luminance("surface")).toBeLessThan(luminance("surface-raised"));
    } else {
      expect(luminance("bg-subtle")).toBeLessThan(luminance("bg"));
      expect(luminance("bg")).toBeLessThanOrEqual(luminance("surface"));
    }
  });

  it("содержит все имена из контракта", () => {
    for (const name of CONTRACT) expect(() => t(name), name).not.toThrow();
  });
});

describe("контрасты стайлгайда §2", () => {
  // [токен, к чему считаем, светлая, тёмная]: текст и статусные цвета — к surface, control и focus-ring — к bg.
  const EXPECTED: [string, string, number, number][] = [
    ["fg", "surface", 16.7, 15.9],
    ["fg-muted", "surface", 7.0, 8.9],
    ["fg-subtle", "surface", 5.0, 5.5],
    ["accent-fg", "surface", 7.5, 10.5],
    ["info-fg", "surface", 7.2, 10.8],
    ["success-fg", "surface", 6.9, 11.0],
    ["warning-fg", "surface", 9.9, 10.5],
    ["danger-fg", "surface", 8.0, 10.2],
    ["info", "surface", 5.4, 7.9],
    ["success", "surface", 5.2, 8.2],
    ["warning", "surface", 5.6, 7.6],
    ["control", "bg", 3.2, 3.8],
    ["focus-ring", "bg", 3.4, 8.0],
  ];

  it.each(EXPECTED)("%s на %s", (name, base, light, dark) => {
    // Допуск 0.12: стайлгайд округлял до одного знака и считал по hex, тест — по OKLCH.
    expect(Math.abs(contrastRatio(token("light", name), token("light", base)) - light)).toBeLessThan(0.12);
    expect(Math.abs(contrastRatio(token("dark", name), token("dark", base)) - dark)).toBeLessThan(0.12);
  });

  it("белый на accent — 5.7, на danger — 6.1 в обеих темах", () => {
    for (const theme of THEMES) {
      expect(contrastRatio(token(theme, "fg-on-accent"), token(theme, "accent"))).toBeCloseTo(5.7, 0);
      expect(contrastRatio(token(theme, "fg-on-accent"), token(theme, "danger"))).toBeCloseTo(6.1, 0);
    }
  });
});

describe("семантика", () => {
  it("тёмная тема задана для каждого токена светлой (пара light-dark или общее значение)", () => {
    const names = Object.keys(semantic);
    expect(names.length).toBeGreaterThanOrEqual(CONTRACT.length);
    for (const name of names) {
      const dark = resolveValue(semantic[name] as string, "dark");
      const light = resolveValue(semantic[name] as string, "light");
      expect(dark, name).toBeDefined();
      expect(light, name).toBeDefined();
    }
  });

  it("тема-независимые токены — только заливки и белый текст на них", () => {
    const same = Object.keys(semantic).filter((name) => {
      const a = resolveValue(semantic[name] as string, "light");
      const b = resolveValue(semantic[name] as string, "dark");
      return formatOklch(a) === formatOklch(b);
    });
    expect(same.sort()).toEqual(
      [
        "--color-accent",
        "--color-accent-hover",
        "--color-danger",
        "--color-danger-hover",
        "--color-fg-on-accent",
      ].sort(),
    );
  });

  it("каждый семантический токен зарегистрирован в @theme для Tailwind", () => {
    for (const name of Object.keys(semantic)) {
      expect(tokensCss, name).toContain(`${name}: var(${name});`);
    }
  });

  it("data-theme переключает color-scheme, по умолчанию — системная тема", () => {
    expect(tokensCss).toMatch(/:root\s*\{\s*color-scheme: light dark;/);
    expect(tokensCss).toMatch(/\[data-theme="light"\]\s*\{\s*color-scheme: light;/);
    expect(tokensCss).toMatch(/\[data-theme="dark"\]\s*\{\s*color-scheme: dark;/);
  });

  it("success, warning, danger и info не переопределяются пространствами вузов", () => {
    for (const rule of rules.filter((r) => r.selector.includes("data-space"))) {
      const names = Object.keys(rule.declarations);
      expect(
        names.filter((n) => !n.startsWith("--color-accent")),
        rule.selector,
      ).toEqual([]);
    }
  });
});

describe("запасной блок без light-dark()", () => {
  /** Светлая сторона значения: light-dark(a, b) → a, на любой глубине. */
  function lightSide(value: string): string {
    let out = "";
    let index = 0;
    while (index < value.length) {
      if (value.startsWith("light-dark(", index)) {
        let depth = 1;
        let end = index + "light-dark(".length;
        while (depth > 0) {
          if (value[end] === "(") depth++;
          if (value[end] === ")") depth--;
          end++;
        }
        const [light] = splitTopLevel(value.slice(index + "light-dark(".length, end - 1), ",");
        out += lightSide(light as string);
        index = end;
      } else {
        out += value[index];
        index++;
      }
    }
    return out;
  }

  const start = tokensCss.indexOf("@supports not (color: light-dark(white, black))");
  const open = tokensCss.indexOf(":root {", start);
  const close = tokensCss.indexOf("\n  }", open);
  const body = start < 0 ? "" : tokensCss.slice(open + ":root {".length, close);
  const fallback: Declarations = {};
  for (const statement of splitTopLevel(body, ";")) {
    const colon = statement.indexOf(":");
    if (colon > 0)
      fallback[statement.slice(0, colon).trim()] = statement
        .slice(colon + 1)
        .replace(/\s+/g, " ")
        .trim();
  }
  const themed = Object.entries(palette).filter(
    ([name, value]) => name !== "color-scheme" && value.includes("light-dark("),
  );

  it("есть и покрывает каждый токен с парой light-dark", () => {
    expect(start).toBeGreaterThan(0);
    expect(Object.keys(fallback).sort()).toEqual(themed.map(([name]) => name).sort());
  });

  it.each(themed)("%s — светлое значение совпадает с парой выше", (name, value) => {
    expect(fallback[name]).toBe(lightSide(value));
  });
});

describe("акцент пространства вуза", () => {
  it("реестр: окно тона 235°…345° и уникальные id", () => {
    expect(new Set(UNIVERSITY_ACCENTS.map((row) => row.id)).size).toBe(UNIVERSITY_ACCENTS.length);
    for (const row of UNIVERSITY_ACCENTS) {
      expect(row.hue, row.id).toBeGreaterThanOrEqual(ACCENT_HUE_WINDOW.min);
      expect(row.hue, row.id).toBeLessThanOrEqual(ACCENT_HUE_WINDOW.max);
    }
  });

  it("тон вне окна отвергается", () => {
    expect(isAccentHueAllowed(80)).toBe(false);
    expect(isAccentHueAllowed(156)).toBe(false);
    expect(isAccentHueAllowed(228)).toBe(false);
    expect(isAccentHueAllowed(25)).toBe(false);
    expect(isAccentHueAllowed(262.2)).toBe(true);
    expect(isAccentHueAllowed(Number.NaN)).toBe(false);
    expect(selectAccent(80)).toEqual({ kind: "default" });
    expect(selectAccent(null)).toEqual({ kind: "default" });
    expect(selectAccent(292)).toEqual({ kind: "registry", id: "cu" });
    expect(selectAccent(300).kind).toBe("custom");
  });

  it("ВШЭ в реестре совпадает со шкалой в tokens.css по умолчанию", () => {
    const scale = accentScale(262.2);
    for (const step of Object.keys(LADDER_LIGHTNESS).map(Number) as LadderStep[]) {
      const css = parseOklch(palette[`--palette-hse-${step}`] ?? "") as Oklch;
      expect(formatOklch(scale[step]), `hse-${step}`).toBe(formatOklch(css));
    }
  });

  describe.each(UNIVERSITY_ACCENTS)("$shortTitle", (row) => {
    const scale = accentScale(row.hue);

    it("вся шкала в гамуте sRGB", () => {
      for (const step of Object.keys(scale)) {
        expect(isInSrgbGamut(scale[Number(step) as LadderStep]), `шаг ${step}`).toBe(true);
      }
    });

    it.each(THEMES)("белый текст на заливке акцента и на нажатии — не меньше 4.5:1 (%s)", () => {
      const white: Oklch = { l: 1, c: 0, h: 0, alpha: 1 };
      expect(contrastRatio(white, scale[600])).toBeGreaterThanOrEqual(WCAG_AA_TEXT);
      expect(contrastRatio(white, scale[700])).toBeGreaterThanOrEqual(WCAG_AA_TEXT);
    });

    it.each(THEMES)("accent-fg читается на всех поверхностях и на accent-subtle (%s)", (theme) => {
      const fg = theme === "light" ? scale[700] : scale[300];
      const subtle = theme === "light" ? scale[50] : scale[950];
      for (const surface of SURFACES) {
        expect(contrastRatio(fg, token(theme, surface)), surface).toBeGreaterThanOrEqual(WCAG_AA_TEXT);
      }
      expect(contrastRatio(fg, subtle)).toBeGreaterThanOrEqual(6.4);
    });

    it("графика заливки акцента различима на фоне (3:1)", () => {
      for (const theme of THEMES) {
        expect(contrastRatio(scale[600], token(theme, "bg"))).toBeGreaterThanOrEqual(WCAG_AA_NON_TEXT);
      }
    });

    it("блок data-space в tokens.css совпадает с вычисленной шкалой", () => {
      const vars = accentVariables(row.hue);
      const block = rules.find((r) =>
        r.selector.split(",").some((s) => s.trim() === `[data-space="${row.id}"]`),
      );
      expect(block, `нет блока data-space="${row.id}" в tokens.css`).toBeDefined();
      if (row.id === "hse") {
        // ВШЭ ссылается на палитру, а не на литералы: сверяем разрешённые значения.
        for (const name of Object.keys(vars)) {
          for (const theme of THEMES) {
            const css = resolveValue(block?.declarations[name] as string, theme);
            const computed = resolveValue(vars[name] as string, theme);
            expect(formatOklch(css), `${name} ${theme}`).toBe(formatOklch(computed));
          }
        }
      } else {
        expect(block?.declarations).toEqual(vars);
      }
    });
  });
});

describe("движение", () => {
  it("общее правило сокращённого движения: системная настройка и «спокойный режим»", () => {
    expect(baseCss).toContain("@media (prefers-reduced-motion: reduce)");
    expect(baseCss).toContain(':root[data-motion="calm"] *');
    expect(baseCss.match(/animation: none !important/g)?.length).toBeGreaterThanOrEqual(2);
    expect(baseCss.match(/transition-duration: 1ms !important/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("токены движения из стайлгайда §5", () => {
    expect(tokensCss).toContain("--ease-soft: cubic-bezier(0.22, 1, 0.36, 1)");
    expect(tokensCss).toContain("--ease-in-soft: cubic-bezier(0.32, 0, 0.67, 0)");
    expect(tokensCss).toContain("--ease-in-out-soft: cubic-bezier(0.65, 0, 0.35, 1)");
    expect(baseCss).toContain("transition-duration: 120ms");
    expect(baseCss).toContain("transition-duration: 180ms");
    expect(baseCss).toContain("transition-duration: 280ms");
    expect(tokensCss).toContain("2400ms");
  });
});

describe("типографика", () => {
  it("минимальный кегль — 13 px, поле ввода — 16 px", () => {
    const size = (role: string) => {
      const match = new RegExp(
        `@utility type-${role} \\{\\s*(?:font-family[^;]*;\\s*)?font-size: ([\\d.]+)rem`,
      ).exec(baseCss);
      return match ? Number(match[1]) * 16 : Number.NaN;
    };
    for (const role of [
      "display",
      "title",
      "heading",
      "subheading",
      "body",
      "ui",
      "label",
      "caption",
      "mono",
    ]) {
      expect(size(role), role).toBeGreaterThanOrEqual(13);
    }
    expect(size("body")).toBe(16);
    expect(size("ui")).toBe(15);
  });
});

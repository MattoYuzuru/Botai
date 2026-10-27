/**
 * Цветовая математика дизайн-системы: OKLCH → sRGB, гамут и контраст WCAG 2.x.
 * Используется tokens.test.ts, реестром акцентов вузов и витриной /styleguide
 * (hex считается из токенов, а не хранится руками, поэтому документация не расходится с CSS).
 */

export interface Oklch {
  l: number;
  c: number;
  h: number;
  alpha: number;
}

export interface Rgb {
  r: number;
  g: number;
  b: number;
}

const OKLCH_RE = /^oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)(?:deg)?\s*(?:\/\s*([\d.]+)(%?))?\s*\)$/i;

/** Разбирает строку вида «oklch(L C H / A)». Возвращает null, если формат другой. */
export function parseOklch(value: string): Oklch | null {
  const match = OKLCH_RE.exec(value.trim());
  if (!match) return null;
  const [, l = "0", lPercent, c = "0", h = "0", a, aPercent] = match;
  return {
    l: Number(l) / (lPercent ? 100 : 1),
    c: Number(c),
    h: Number(h),
    alpha: a === undefined ? 1 : Number(a) / (aPercent ? 100 : 1),
  };
}

export function formatOklch({ l, c, h, alpha }: Oklch): string {
  const base = `${round(l, 3)} ${round(c, 3)} ${round(h, 1)}`;
  // design-lint-ignore: сериализация значения токена акцента вуза, это не цвет интерфейса
  return alpha < 1 ? `oklch(${base} / ${round(alpha, 2)})` : `oklch(${base})`;
}

const round = (x: number, digits: number) => Number(x.toFixed(digits));

/** Линейный sRGB без обрезки по гамуту. */
function toLinearSrgb({ l, c, h }: Oklch): [number, number, number] {
  const a = c * Math.cos((h * Math.PI) / 180);
  const b = c * Math.sin((h * Math.PI) / 180);
  const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s_ = (l - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
    -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
    -0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_,
  ];
}

const clamp01 = (x: number) => Math.min(1, Math.max(0, x));
const encode = (x: number) => (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055);
const decode = (x: number) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);

/** Цвет внутри гамута sRGB; допуск — на округление в линейном свете (по умолчанию 0.0005). */
export function isInSrgbGamut(color: Oklch, tolerance = 0.0005): boolean {
  return toLinearSrgb(color).every((x) => x >= -tolerance && x <= 1 + tolerance);
}

/** Наибольшая насыщенность при заданных L и H, которая ещё лежит в гамуте sRGB. */
export function maxChroma(l: number, h: number): number {
  let lo = 0;
  let hi = 0.4;
  for (let i = 0; i < 40; i++) {
    const mid = (lo + hi) / 2;
    if (isInSrgbGamut({ l, c: mid, h, alpha: 1 })) lo = mid;
    else hi = mid;
  }
  return lo;
}

/** sRGB (0..1, гамма-кодированный); вне гамута — обрезка по каналам. */
export function toRgb(color: Oklch): Rgb {
  const [r, g, b] = toLinearSrgb(color).map((x) => clamp01(encode(clamp01(x)))) as [number, number, number];
  return { r, g, b };
}

export function toHex(color: Oklch): string {
  const { r, g, b } = toRgb(color);
  return `#${[r, g, b]
    .map((x) =>
      Math.round(x * 255)
        .toString(16)
        .padStart(2, "0"),
    )
    .join("")}`;
}

export function parseHex(hex: string): Rgb {
  const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255) as [
    number,
    number,
    number,
  ];
  return { r, g, b };
}

/** Накладывает полупрозрачный цвет на непрозрачный фон (в sRGB, как делает браузер). */
export function composite(top: Oklch, bottom: Oklch): Rgb {
  const t = toRgb(top);
  const b = toRgb(bottom);
  const a = top.alpha;
  return { r: t.r * a + b.r * (1 - a), g: t.g * a + b.g * (1 - a), b: t.b * a + b.b * (1 - a) };
}

/** Относительная яркость по WCAG 2.x. */
export function relativeLuminance(rgb: Rgb): number {
  return 0.2126 * decode(rgb.r) + 0.7152 * decode(rgb.g) + 0.0722 * decode(rgb.b);
}

/** Контраст WCAG 2.x (1..21). Полупрозрачный передний план накладывается на фон. */
export function contrastRatio(foreground: Oklch, background: Oklch): number {
  const fg = foreground.alpha < 1 ? composite(foreground, background) : toRgb(foreground);
  const y1 = relativeLuminance(fg);
  const y2 = relativeLuminance(toRgb(background));
  const [hi, lo] = y1 > y2 ? [y1, y2] : [y2, y1];
  return (hi + 0.05) / (lo + 0.05);
}

export const WCAG_AA_TEXT = 4.5;
export const WCAG_AA_NON_TEXT = 3;

/** sRGB (0..1) → OKLCH. Нужен витрине: браузер отдаёт вычисленный цвет как rgb() или color(srgb …). */
export function rgbToOklch({ r, g, b }: Rgb): Oklch {
  const [lr, lg, lb] = [r, g, b].map(decode) as [number, number, number];
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  return { l: L, c: Math.hypot(A, B), h: ((Math.atan2(B, A) * 180) / Math.PI + 360) % 360, alpha: 1 };
}

/**
 * Вычисленный цвет браузера (`getComputedStyle(el).backgroundColor`) → OKLCH.
 * Понимает oklch(), rgb()/rgba() и color(srgb r g b). Остальное — null.
 */
export function parseCssColor(value: string): Oklch | null {
  const v = value.trim();
  const direct = parseOklch(v);
  if (direct) return direct;
  const rgb = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/.exec(v);
  if (rgb) {
    const color = rgbToOklch({ r: Number(rgb[1]) / 255, g: Number(rgb[2]) / 255, b: Number(rgb[3]) / 255 });
    const a = rgb[4];
    return {
      ...color,
      alpha: a === undefined ? 1 : a.endsWith("%") ? Number.parseFloat(a) / 100 : Number(a),
    };
  }
  const srgb = /^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\s*\)$/.exec(v);
  if (srgb) {
    const color = rgbToOklch({ r: Number(srgb[1]), g: Number(srgb[2]), b: Number(srgb[3]) });
    return { ...color, alpha: srgb[4] === undefined ? 1 : Number(srgb[4]) };
  }
  return null;
}

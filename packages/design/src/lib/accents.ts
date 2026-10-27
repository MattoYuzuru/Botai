/**
 * Реестр акцентов вузов (styleguide §2 «Акцент пространства вуза»).
 * Пространство задаёт только шкалу акцента; success/warning/danger/info, нейтрали, тени и фокус
 * от вуза не зависят. Новый вуз — одна строка реестра, а не новый набор hex.
 */
import { formatOklch, maxChroma, type Oklch } from "./color";

/** Допустимое окно тона акцента: оно не задевает Янтарь (80), Хвою (156), Лазурь (228) и Клюкву (25). */
export const ACCENT_HUE_WINDOW = { min: 235, max: 345 } as const;

export function isAccentHueAllowed(hue: number): boolean {
  return Number.isFinite(hue) && hue >= ACCENT_HUE_WINDOW.min && hue <= ACCENT_HUE_WINDOW.max;
}

export interface UniversityAccent {
  id: string;
  title: string;
  shortTitle: string;
  hue: number;
  /** Тон подтверждён по брендбуку вуза. */
  verified: boolean;
}

export const UNIVERSITY_ACCENTS = [
  { id: "hse", title: "Высшая школа экономики", shortTitle: "ВШЭ", hue: 262.2, verified: true },
  { id: "cu", title: "Центральный университет", shortTitle: "ЦУ", hue: 292, verified: false },
  { id: "reserve", title: "Резерв", shortTitle: "Резерв", hue: 325, verified: false },
] as const satisfies readonly UniversityAccent[];

export type UniversityAccentId = (typeof UNIVERSITY_ACCENTS)[number]["id"];

/** Сводный экран «Все вузы»: акцент ВШЭ, цветом вузы не различаются. */
export const ALL_SPACES_ID = "all";
export const DEFAULT_ACCENT_ID: UniversityAccentId = "hse";

/** Ступени светлоты общие для всех шкал. */
export const LADDER_LIGHTNESS = {
  50: 0.972,
  100: 0.945,
  200: 0.895,
  300: 0.825,
  400: 0.735,
  500: 0.63,
  600: 0.52,
  700: 0.455,
  800: 0.385,
  900: 0.315,
  950: 0.25,
} as const;

export type LadderStep = keyof typeof LADDER_LIGHTNESS;
export const LADDER_STEPS = Object.keys(LADDER_LIGHTNESS).map(Number) as LadderStep[];

/** Профиль насыщенности шкалы ВШЭ; у других вузов он зажимается до 0.985 от предела гаммы. */
export const ACCENT_CHROMA_PROFILE: Record<LadderStep, number> = {
  50: 0.013,
  100: 0.026,
  200: 0.05,
  300: 0.086,
  400: 0.135,
  500: 0.175,
  600: 0.18,
  700: 0.165,
  800: 0.135,
  900: 0.111,
  950: 0.075,
};

const GAMUT_MARGIN = 0.985;

export function accentStep(hue: number, step: LadderStep): Oklch {
  const l = LADDER_LIGHTNESS[step];
  const limit = Math.round(GAMUT_MARGIN * maxChroma(l, hue) * 1000) / 1000;
  return { l, c: Math.min(ACCENT_CHROMA_PROFILE[step], limit), h: hue, alpha: 1 };
}

export function accentScale(hue: number): Record<LadderStep, Oklch> {
  return Object.fromEntries(LADDER_STEPS.map((step) => [step, accentStep(hue, step)])) as Record<
    LadderStep,
    Oklch
  >;
}

/**
 * Пять семантических переменных акцента для тона. Заливка — ступень 600 в обеих темах,
 * accent-fg — 700 в светлой и 300 в тёмной, accent-subtle — 50 и 950.
 */
export function accentVariables(hue: number): Record<string, string> {
  const scale = accentScale(hue);
  const at = (step: LadderStep) => formatOklch(scale[step]);
  return {
    "--color-accent": at(600),
    "--color-accent-hover": at(700),
    "--color-accent-subtle": `light-dark(${at(50)}, ${at(950)})`,
    "--color-accent-fg": `light-dark(${at(700)}, ${at(300)})`,
  };
}

/** Строка реестра по тону из API (`accentHue`); допуск 0.5° на округление. */
export function findAccentByHue(hue: number): (typeof UNIVERSITY_ACCENTS)[number] | undefined {
  return UNIVERSITY_ACCENTS.find((row) => Math.abs(row.hue - hue) < 0.5);
}

/**
 * Как применить тон пространства: id из реестра (атрибут data-space) либо вычисленные
 * переменные для тона из окна. Тон вне окна отвергается — остаётся акцент ВШЭ.
 */
export type AccentSelection =
  | { kind: "registry"; id: UniversityAccentId }
  | { kind: "custom"; hue: number; variables: Record<string, string> }
  | { kind: "default" };

export function selectAccent(hue: number | null | undefined): AccentSelection {
  if (hue === null || hue === undefined || !isAccentHueAllowed(hue)) return { kind: "default" };
  const row = findAccentByHue(hue);
  if (row) return { kind: "registry", id: row.id };
  return { kind: "custom", hue, variables: accentVariables(hue) };
}

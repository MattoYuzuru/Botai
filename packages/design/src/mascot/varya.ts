/**
 * Типизированный доступ к вендоренному renderer (`vendor/varya.js`, docs/design/vendor.md).
 * Файл renderer — классический скрипт, он кладёт `Varya` в глобальный объект; здесь он подключается
 * побочным импортом и читается с проверкой: повреждённый или отсутствующий ресурс даёт `null`, а экраны
 * от картинки не зависят (mascot.md §1).
 */
import "./vendor/varya.js";
import type { MascotAct, MascotLoop, MascotSkinId, MascotTheme } from "./types";

export interface VaryaSituationDef {
  id: string;
  label: string;
  /** Реплики оригинальных ситуаций doodle_vibe; Botai их не показывает (в них придуманные числа). */
  lines: readonly string[];
  anim: string;
  hold?: { eyes?: string; props?: readonly string[]; loop?: MascotLoop };
  pose: { eyes?: string; props?: readonly string[]; night?: boolean; legs?: string };
}

export interface VaryaMountOptions {
  theme?: MascotTheme;
  situation?: string;
  skin?: MascotSkinId | null;
  still?: boolean;
  idle?: boolean;
  idleRange?: readonly [number, number];
  lookRange?: readonly [number, number];
  random?: () => number;
  onAnim?: (animation: string | null) => void;
  /** Реплика оригинальных ситуаций doodle_vibe. Botai её не передаёт и не показывает. */
  onSay?: (label: string, text: string, situation: { id: string }) => void;
}

export interface VaryaHandle {
  readonly svg: SVGSVGElement;
  readonly situation: string;
  readonly theme: MascotTheme;
  readonly skin: MascotSkinId | null;
  readonly still: boolean;
  react: () => void;
  act: (name: MascotAct) => void;
  setSituation: (id: string) => void;
  setTheme: (name: MascotTheme) => void;
  setSkin: (name: MascotSkinId | null) => void;
  setStill: (value: boolean) => void;
  setLoop: (name: MascotLoop | null) => void;
  /** Вернуться к «держащему» виду ситуации (hold) или к её позе (still). */
  settle: () => void;
  staticSVG: () => string;
  destroy: () => void;
}

export interface VaryaSvgOptions {
  situation?: string;
  theme?: MascotTheme;
  skin?: MascotSkinId | null;
  eyes?: string;
  props?: readonly string[];
  night?: boolean;
  legs?: string;
  size?: number;
  id?: string;
}

export interface VaryaApi {
  SITUATIONS: readonly VaryaSituationDef[];
  SKINS: readonly string[];
  ACTS: Readonly<Record<string, { anim: string; ms: number }>>;
  VIEWBOX: string;
  mount: (element: Element, options?: VaryaMountOptions) => VaryaHandle;
  svg: (options?: VaryaSvgOptions) => string;
  portrait: (options?: { maskable?: boolean; transparent?: boolean }) => string;
}

/** Renderer, если он загрузился. */
export function getVarya(): VaryaApi | null {
  const api = (globalThis as { Varya?: VaryaApi }).Varya;
  return api && typeof api.mount === "function" ? api : null;
}

/**
 * Детерминированный генератор (mulberry32): idle и сцена пустого чата зависят от зерна, а не от
 * `Math.random()`. Одинаковое зерно — одинаковая последовательность.
 */
export function seededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

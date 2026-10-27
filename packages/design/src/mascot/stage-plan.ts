/**
 * План сцены пустого чата (docs/design/mascot.md §4.2): пауза 6–10 с → одно действие 1.5–2.5 с: пройтись
 * влево-вправо, клюнуть пол, встряхнуться, посмотреть вверх, помахать крылом. Порядок и длительности
 * выводятся из зерна (`nowMs` при открытии чата) без `Math.random()`: одинаковое зерно — одинаковая сцена,
 * а шаг с любым номером считается за O(1).
 */
import { seededRandom } from "./varya";

export type StageAction = "walk" | "peck" | "shake" | "look" | "wave";

export const STAGE_ACTIONS: readonly StageAction[] = ["walk", "peck", "shake", "look", "wave"];
export const STAGE_PAUSE_MS: readonly [number, number] = [6000, 10000];
export const STAGE_ACTION_MS: readonly [number, number] = [1500, 2500];

export interface StageStep {
  index: number;
  action: StageAction;
  /** Пауза перед действием. */
  pauseMs: number;
  /** Длительность действия. */
  durationMs: number;
  /** Для `walk`: в какую сторону идти по возможности (−1 — влево). */
  direction: -1 | 1;
}

/** Смешивает зерно и номер в 32-битное число для независимого генератора шага. */
function mix(seed: number, index: number): number {
  let h = Math.imul(seed >>> 0 ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(index + 1, 0xc2b2ae35);
  h ^= h >>> 15;
  h = Math.imul(h, 0x2c1b3c6d);
  h ^= h >>> 12;
  return h >>> 0;
}

/** Порядок действий одного круга: перестановка пяти действий по зерну (Фишер — Йетс). */
function cycleOrder(seed: number, cycle: number): StageAction[] {
  const random = seededRandom(mix(seed, 1000 + cycle));
  const order = [...STAGE_ACTIONS];
  for (let i = order.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    const held = order[i] as StageAction;
    order[i] = order[j] as StageAction;
    order[j] = held;
  }
  return order;
}

const between = (random: () => number, [low, high]: readonly [number, number]) =>
  Math.round(low + random() * (high - low));

/** Шаг сцены с номером `index`. Подряд одно и то же действие не повторяется, за круг из пяти шагов — все пять. */
export function stageStep(seed: number, index: number): StageStep {
  const cycle = Math.floor(index / STAGE_ACTIONS.length);
  const position = index % STAGE_ACTIONS.length;
  const order = cycleOrder(seed, cycle);
  // Круг не начинается тем, чем закончился прошлый: меняем первые два действия местами.
  if (cycle > 0) {
    const previousLast = cycleOrder(seed, cycle - 1)[STAGE_ACTIONS.length - 1];
    if (order[0] === previousLast) {
      const first = order[0] as StageAction;
      order[0] = order[1] as StageAction;
      order[1] = first;
    }
  }
  const random = seededRandom(mix(seed, index));
  return {
    index,
    action: order[position] as StageAction,
    pauseMs: between(random, STAGE_PAUSE_MS),
    durationMs: between(random, STAGE_ACTION_MS),
    direction: random() < 0.5 ? -1 : 1,
  };
}

/** Первые `count` шагов. */
export function stagePlan(seed: number, count: number): StageStep[] {
  return Array.from({ length: count }, (_, index) => stageStep(seed, index));
}

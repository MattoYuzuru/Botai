/**
 * Значение оценки словами и доля полосы (D-039, D-043): «8» + «из 10», когда у числа есть максимум; иначе шкала
 * источника. Ничего не пересчитывается между шкалами и не усредняется; доля `valueNum / scaleMax` — та же пара
 * чисел одной записи. Общее для «Учёбы», «Заданий» и «Сегодня».
 */

const PLAIN_NUMBER = /^\s*[-−]?\d+(?:[.,]\d+)?\s*$/;

/** Верх шкалы числом по-русски: «10», «100», «4,5». */
export const formatMax = (max: number): string => max.toLocaleString("ru-RU", { maximumFractionDigits: 2 });

/** Значение и шкала записи: оценки, оценки задания или записи «Новое». */
export interface ScoreInput {
  value: string;
  scale?: string | null;
  scaleMax?: number | null;
}

export interface ScoreText {
  /** Значение, как его напечатал источник: «8», «7,5», «зачтено». */
  value: string;
  /** Шкала рядом со значением: «из 10» (если источник дал максимум числом) или слово шкалы источника. */
  suffix: string | null;
}

/**
 * Значение и шкала словами: «8» + «из 10», когда у числа есть максимум; иначе шкала источника («10 баллов»,
 * «зачёт»). Если источник уже написал «8 из 10» или «86%», текст не дополняется.
 */
export function scoreText(grade: ScoreInput): ScoreText {
  const value = grade.value.trim();
  const max = grade.scaleMax ?? null;
  if (max !== null && max > 0 && PLAIN_NUMBER.test(value)) {
    return { value, suffix: `из ${formatMax(max)}` };
  }
  const scale = grade.scale?.trim();
  if (!scale || scale.toLowerCase() === value.toLowerCase()) return { value, suffix: null };
  return { value, suffix: scale };
}

/** Одной строкой: «8 из 10», «зачтено», «86 · 100 баллов». */
export function scoreLabel(grade: ScoreInput): string {
  const { value, suffix } = scoreText(grade);
  if (!suffix) return value;
  return suffix.startsWith("из ") ? `${value} ${suffix}` : `${value} · ${suffix}`;
}

/** Доля заполнения полосы для одной записи: только если источник дал и число, и максимум. */
export function fillOf(grade: { valueNum?: number | null; scaleMax?: number | null }): number | null {
  const valueNum = grade.valueNum ?? null;
  const scaleMax = grade.scaleMax ?? null;
  if (valueNum === null || scaleMax === null || !Number.isFinite(valueNum) || !(scaleMax > 0)) return null;
  return Math.min(1, Math.max(0, valueNum / scaleMax));
}

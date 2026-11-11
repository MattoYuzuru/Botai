/**
 * Значение оценки словами и доля полосы (D-039, D-043): «8» + «из 10», когда шкала известна числом; иначе подпись
 * источника отдельно, приглушённо. Ничего не пересчитывается между шкалами и не усредняется; доля
 * `valueNum / scaleMax` — та же пара чисел одной записи. Общее для «Учёбы», «Заданий» и «Сегодня».
 *
 * Поле `scale` у старых playbook'ов — не всегда шкала: там бывает подпись («оценка Smart LMS», «баллы за курс»).
 * Поэтому «из N» пишется, только если максимум дан числом (`scaleMax`) или текст шкалы читается как шкала
 * («из 20», «/ 10», «10 баллов», «100-балльная»). Любой другой текст — подпись (`caption`): одна строка с
 * многоточием, ширину колонки он не задаёт и по словам не переносится.
 */

const PLAIN_NUMBER = /^\s*[-−]?\d+(?:[.,]\d+)?\s*$/;
/** «из 20», «/10», «/ 5». */
const OUT_OF = /^(?:из|\/)\s*(\d+(?:[.,]\d+)?)$/i;
/** «10 баллов», «100-балльная», «5-балльная шкала», «10 б.». */
const POINTS = /^(\d+(?:[.,]\d+)?)\s*-?\s*(?:балл\p{L}*|б\.?)(?:\s+шкал\p{L}*)?$/iu;

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
  /** «из 10» — только когда шкала известна числом; рядом со значением. */
  suffix: string | null;
  /** Подпись источника, которая не является шкалой («оценка Smart LMS», «зачёт»): приглушённо, в одну строку. */
  caption: string | null;
}

/** Верх шкалы из текста шкалы, если текст — действительно шкала. */
export function parseScaleMax(scale: string | null | undefined): number | null {
  const text = scale?.trim();
  if (!text) return null;
  const match = OUT_OF.exec(text) ?? POINTS.exec(text);
  if (!match?.[1]) return null;
  const max = Number(match[1].replace(",", "."));
  return Number.isFinite(max) && max > 0 ? max : null;
}

/**
 * Значение, «из N» и подпись: «8» + «из 10», когда у числа есть максимум (`scaleMax` или шкала словами); иначе
 * подпись источника отдельно. Если источник уже написал «8 из 10» или «86%», значение не дополняется.
 */
export function scoreText(grade: ScoreInput): ScoreText {
  const value = grade.value.trim();
  const scale = grade.scale?.trim() || null;
  const parsed = parseScaleMax(scale);
  const max = grade.scaleMax ?? parsed;
  if (max !== null && max > 0 && PLAIN_NUMBER.test(value)) {
    return { value, suffix: `из ${formatMax(max)}`, caption: parsed !== null ? null : scale };
  }
  if (!scale || parsed !== null || scale.toLowerCase() === value.toLowerCase()) {
    return { value, suffix: null, caption: null };
  }
  return { value, suffix: null, caption: scale };
}

/** Одной строкой для тесных мест (строка «Нового», зачётки, метка балла): «8 из 10», «зачтено». Без подписи. */
export function scoreLabel(grade: ScoreInput): string {
  const { value, suffix } = scoreText(grade);
  return suffix ? `${value} ${suffix}` : value;
}

/** Доля заполнения полосы для одной записи: только если источник дал и число, и максимум. */
export function fillOf(grade: { valueNum?: number | null; scaleMax?: number | null }): number | null {
  const valueNum = grade.valueNum ?? null;
  const scaleMax = grade.scaleMax ?? null;
  if (valueNum === null || scaleMax === null || !Number.isFinite(valueNum) || !(scaleMax > 0)) return null;
  return Math.min(1, Math.max(0, valueNum / scaleMax));
}

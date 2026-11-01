/**
 * Печать ответа агента: текст появляется прогрессивно, по code points, как в ChatGPT/Claude, но заметно быстрее.
 * Скорость фиксированная для обычного ответа и растёт для длинного: общая длительность не больше четырёх секунд.
 * State обновляется не чаще 30 раз в секунду (столько раз в секунду заново разбирается markdown) — кадры rAF
 * нужны только чтобы попасть в нужный момент, а не чтобы перерисовывать React.
 *
 * Решение «печатать или нет» принимается один раз при монтировании: блок сначала приходит живым из снимка
 * задания, затем тот же блок (тот же ключ) приходит из сохранённой истории — печать от этого не начинается
 * заново. Исторический блок при открытии чата показывается целиком сразу.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

/** Обычная скорость печати, символов в секунду. */
export const TYPEWRITER_CPS = 320;
/** Дольше этого ответ не печатается: длинный текст идёт быстрее. */
export const TYPEWRITER_MAX_MS = 4_000;
/** Короткая реплика всё равно печатается хотя бы столько: иначе эффект читается как «появилось сразу». */
export const TYPEWRITER_MIN_MS = 240;
/** Не чаще 30 обновлений state в секунду. */
const PAINT_INTERVAL_MS = 1000 / 30;

/** Сколько миллисекунд займёт появление `chars` code points. */
export function typingDurationMs(chars: number): number {
  if (chars <= 0) return 0;
  const natural = (chars / TYPEWRITER_CPS) * 1000;
  return Math.round(Math.min(TYPEWRITER_MAX_MS, Math.max(TYPEWRITER_MIN_MS, natural)));
}

export interface Typewriter {
  /** Видимый сейчас префикс ответа (по code points). */
  text: string;
  /** Печать ещё идёт: сообщение рисуется как `streaming`, с курсором. */
  typing: boolean;
  /** Показать всё сразу — клик по сообщению. */
  finish: () => void;
}

/**
 * Прогрессивная печать `source`. `animate` — блок пришёл живым; `calm` — сокращённое движение
 * (`useCalmMotion`): тогда сразу весь текст, без rAF и таймеров. Оба решения фиксируются при монтировании.
 */
export function useTypewriter(source: string, animate: boolean, calm: boolean): Typewriter {
  const chars = useMemo(() => Array.from(source), [source]);
  const total = chars.length;
  // Первое решение — единственное: живой блок дописывается до конца, даже если проп `animate` станет false,
  // а исторический не начинает печататься, если живой снимок пришёл позже.
  const [animated] = useState(() => animate && !calm);
  const [shown, setShown] = useState(() => (animate && !calm ? 0 : total));
  const shownRef = useRef(shown);
  const frameRef = useRef(0);

  const paint = useCallback((next: number) => {
    shownRef.current = next;
    setShown(next);
  }, []);

  const finish = useCallback(() => {
    cancelAnimationFrame(frameRef.current);
    frameRef.current = 0;
    if (shownRef.current < total) paint(total);
  }, [paint, total]);

  useEffect(() => {
    // Спокойный режим могут включить посреди ответа: печать заканчивается сразу, без ожидания таймеров.
    if (calm) {
      if (shownRef.current < total) paint(total);
      return;
    }
    if (!animated || shownRef.current >= total) return;

    // Догоняем новый полный текст с текущей позиции: streaming-блок может дорасти тем же `id`.
    const from = shownRef.current;
    const remaining = total - from;
    const duration = typingDurationMs(remaining);
    let startedAt: number | null = null;
    let lastPaint = Number.NEGATIVE_INFINITY;

    const step = (now: number) => {
      frameRef.current = 0;
      if (shownRef.current >= total) return;
      startedAt ??= now;
      const next = Math.min(total, from + Math.ceil(((now - startedAt) / duration) * remaining));
      if (next >= total) {
        paint(total);
        return;
      }
      if (next > shownRef.current && now - lastPaint >= PAINT_INTERVAL_MS) {
        lastPaint = now;
        paint(next);
      }
      frameRef.current = requestAnimationFrame(step);
    };
    frameRef.current = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = 0;
    };
  }, [animated, calm, paint, total]);

  // Размонтирование и переход на другой блок не оставляют за собой кадров.
  useEffect(() => () => cancelAnimationFrame(frameRef.current), []);

  const text = useMemo(() => chars.slice(0, shown).join(""), [chars, shown]);
  return { text, typing: animated && shown < total, finish };
}

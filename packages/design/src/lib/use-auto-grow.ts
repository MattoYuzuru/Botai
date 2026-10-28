import { type RefObject, useCallback, useLayoutEffect } from "react";

/**
 * Авто-рост `textarea` между одной строкой и `maxRows`, дальше — прокрутка внутри.
 * Высота считается по `scrollHeight`, поэтому раскладка не прыгает при вводе.
 * Возвращает функцию пересчёта для обработчика `onInput`.
 */
export function useAutoGrow(ref: RefObject<HTMLTextAreaElement | null>, value: unknown, maxRows: number) {
  const resize = useCallback(() => {
    const element = ref.current;
    if (!element) return;
    element.style.height = "auto";
    const line = Number.parseFloat(getComputedStyle(element).lineHeight) || 24;
    const max = line * maxRows;
    element.style.height = `${Math.min(element.scrollHeight, max)}px`;
    element.style.overflowY = element.scrollHeight > max ? "auto" : "hidden";
  }, [ref, maxRows]);

  // value в зависимостях: управляемое поле меняется извне (очистка после отправки).
  // biome-ignore lint/correctness/useExhaustiveDependencies: value запускает пересчёт высоты
  useLayoutEffect(resize, [resize, value]);

  // Ширина меняется и при увеличении текста: соседняя кнопка и отступы растут вместе со шрифтом.
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    let width = element.clientWidth;
    let frame: number | null = null;
    const observer = new ResizeObserver(() => {
      const next = element.clientWidth;
      if (next === width) return;
      width = next;
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        frame = null;
        resize();
      });
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      if (frame !== null) window.cancelAnimationFrame(frame);
    };
  }, [ref, resize]);

  return resize;
}

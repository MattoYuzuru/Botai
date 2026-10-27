import { useCallback, useEffect, useRef } from "react";

/** Ближе этого к низу списка считается «прокручено до конца»: новые сообщения тянут список за собой. */
const PINNED_PX = 48;

/**
 * Лента чата держится у последнего сообщения, пока человек сам не прокрутил вверх (так же ведёт себя
 * `ThinkingStream`). `resetKey` — смена чата: новая лента снова начинается снизу. Уменьшение высоты окна
 * (экранная клавиатура) тоже не прячет последнее сообщение.
 */
export function useStickToBottom(changeKey: unknown, resetKey: unknown) {
  const viewport = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const onScroll = useCallback(() => {
    const element = viewport.current;
    if (element) pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < PINNED_PX;
  }, []);

  const toBottom = useCallback(() => {
    const element = viewport.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: новая лента начинается снизу
  useEffect(() => {
    pinned.current = true;
    toBottom();
  }, [resetKey, toBottom]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: пересчёт при любом изменении ленты
  useEffect(() => {
    if (pinned.current) toBottom();
  }, [changeKey, toBottom]);

  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      if (pinned.current) toBottom();
    });
    observer.observe(element);
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    return () => observer.disconnect();
  }, [toBottom]);

  return { viewport, onScroll, toBottom };
}

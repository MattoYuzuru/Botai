import { useCallback, useEffect, useRef, useState } from "react";

/** Ближе этого к низу страницы считается «прокручено до конца»: новые сообщения тянут страницу за собой. */
const PINNED_PX = 48;

const root = (): HTMLElement => document.scrollingElement as HTMLElement;
const distanceToBottom = (): number => {
  const element = root();
  return element.scrollHeight - element.scrollTop - element.clientHeight;
};

/**
 * Лента чата держится у последнего сообщения, пока человек сам не прокрутил вверх. Прокручивается страница
 * целиком (скроллбар у правого края окна), а не вложенный блок. `resetKey` — смена чата: новая лента снова
 * начинается снизу. Рост содержимого и уменьшение высоты окна (экранная клавиатура) тоже не прячут
 * последнее сообщение. `atBottom` нужен отметке прочтения и кнопке «Вниз».
 */
export function useStickToBottom(changeKey: unknown, resetKey: unknown) {
  const content = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const [atBottom, setAtBottom] = useState(true);

  const toBottom = useCallback(() => {
    const element = root();
    element.scrollTop = element.scrollHeight;
  }, []);

  useEffect(() => {
    const onScroll = () => {
      const near = distanceToBottom() < PINNED_PX;
      pinned.current = near;
      setAtBottom(near);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: новая лента начинается снизу
  useEffect(() => {
    pinned.current = true;
    setAtBottom(true);
    toBottom();
  }, [resetKey, toBottom]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: пересчёт при любом изменении ленты
  useEffect(() => {
    if (pinned.current) toBottom();
  }, [changeKey, toBottom]);

  useEffect(() => {
    const element = content.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      if (pinned.current) toBottom();
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [toBottom]);

  return { content, atBottom, toBottom };
}

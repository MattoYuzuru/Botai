import { useCallback, useEffect, useRef, useState } from "react";

/** Ближе этого к низу страницы считается «прокручено до конца»: новые сообщения тянут страницу за собой. */
const PINNED_PX = 48;
/** Клавиши, которыми страницу листают без мыши: после них «ушёл вверх» — решение человека, а не перестройка ленты. */
const SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);

const root = (): HTMLElement => (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
const distanceToBottom = (): number => {
  const element = root();
  return element.scrollHeight - element.scrollTop - element.clientHeight;
};

/**
 * Лента чата держится у последнего сообщения, пока человек сам не прокрутил вверх. Прокручивается страница
 * целиком (скроллбар у правого края окна), а не вложенный блок. `resetKey` — смена чата: новая лента снова
 * начинается снизу. Рост содержимого и уменьшение высоты окна (экранная клавиатура) тоже не прячут
 * последнее сообщение. `atBottom` нужен отметке прочтения и кнопке «Вниз».
 *
 * Отдельное правило про typewriter: пока ответ печатается, markdown перестраивается и высота ленты может
 * скакнуть более чем на `PINNED_PX` без всякой прокрутки человеком (браузер ещё и сам двигает `scrollTop`
 * scroll anchoring'ом). Поэтому «прижатие» снимается только после явного намерения листать — колесо, касание,
 * клавиши прокрутки или перетаскивание полосы, — а не по любому `scroll`-событию. Иначе на телефоне лента
 * отставала на экран от печатающегося ответа (phone-флейк e2e, пойманный после включения typewriter).
 */
export function useStickToBottom(changeKey: unknown, resetKey: unknown) {
  const content = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  /** Человек сам попросил прокрутку: только тогда scroll-событие может снять прижатие. */
  const intent = useRef(false);
  const [atBottom, setAtBottom] = useState(true);

  const toBottom = useCallback(() => {
    // Программная прокрутка вниз — это снова «прижато», а не намерение листать.
    intent.current = false;
    pinned.current = true;
    setAtBottom(true);
    const element = root();
    element.scrollTop = element.scrollHeight;
  }, []);

  useEffect(() => {
    const markIntent = () => {
      intent.current = true;
    };
    const onScroll = () => {
      const distance = distanceToBottom();
      const near = distance < PINNED_PX;
      if (!intent.current) {
        // Перестройка ленты (печать, подгрузка картинок) не отменяет слежение: остаёмся у низа.
        if (near) {
          pinned.current = true;
          setAtBottom(true);
        }
        return;
      }
      pinned.current = near;
      setAtBottom(near);
      // Человек вернулся к последнему сообщению — прижатие снова наше.
      if (near) intent.current = false;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (!SCROLL_KEYS.has(event.key)) return;
      const target = event.target;
      // Стрелки и пробел внутри поля ввода двигают курсор, а не страницу.
      if (target instanceof HTMLElement && target.closest("textarea, input, [contenteditable]")) return;
      markIntent();
    };
    const onPointerDown = (event: PointerEvent) => {
      // Полоса прокрутки окна: нажатие на её полосе — намерение листать (на телефоне это делает touchmove).
      if (event.clientX >= document.documentElement.clientWidth - 1) markIntent();
    };
    onScroll();
    const passive = { passive: true } as const;
    window.addEventListener("scroll", onScroll, passive);
    window.addEventListener("wheel", markIntent, passive);
    window.addEventListener("touchmove", markIntent, passive);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown, passive);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("wheel", markIntent);
      window.removeEventListener("touchmove", markIntent);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
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
    // Печать меняет высоту не только ленты: markdown перестраивается, появляются быстрые ответы и подписи.
    // Следим и за всей страницей, иначе последний кадр печати может остаться под кромкой окна (phone-флейк e2e).
    if (document.body) observer.observe(document.body);
    return () => observer.disconnect();
  }, [toBottom]);

  return { content, atBottom, toBottom };
}

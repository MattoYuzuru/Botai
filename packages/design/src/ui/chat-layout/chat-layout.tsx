import { type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { cn } from "../../lib/cn";

export interface ChatLayoutProps {
  /** Шапка экрана (`ScreenHeader`). */
  header: ReactNode;
  /** Область сообщений: занимает всё свободное место и прокручивается внутри. */
  children: ReactNode;
  /** Поле ввода (`ChatComposer`) вместе со слоем Вари. */
  composer: ReactNode;
  /** Открыта экранная клавиатура: нижняя панель скрыта, поле прилипает к клавиатуре (styleguide §7). */
  keyboardOpen?: boolean;
  className?: string;
}

/**
 * Рамка экрана чата: шапка сверху, сообщения прокручиваются внутри, поле ввода стоит внизу в зоне большого
 * пальца над нижней панелью. Высота — `100dvh` минус то, что стоит над экраном в потоке (полоса «Нет сети»,
 * свёрнутая работа): так поле ввода всегда на виду и страница не прокручивается. С
 * `interactive-widget=resizes-content` поле поднимается вместе с клавиатурой. На ноутбуке (от 1024 px) нижней
 * панели нет, колонка 46rem по центру.
 */
export function ChatLayout({ header, children, composer, keyboardOpen = false, className }: ChatLayoutProps) {
  const root = useRef<HTMLDivElement>(null);
  const [offset, setOffset] = useState(0);

  useLayoutEffect(() => {
    const element = root.current;
    if (!element) return;
    const measure = () => {
      const top = element.getBoundingClientRect().top + window.scrollY;
      setOffset(Math.max(0, Math.round(top)));
    };
    measure();
    // Полоса над экраном появляется и исчезает: пересчитываем, когда меняется родитель.
    const observer = new ResizeObserver(measure);
    if (element.parentElement) observer.observe(element.parentElement);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  return (
    <div
      ref={root}
      data-chat-layout=""
      style={{ height: `calc(100dvh - ${offset}px)` }}
      className={cn("flex min-h-0 flex-col", className)}
    >
      {header}
      <div className="relative min-h-0 flex-1">{children}</div>
      <div
        data-chat-dock=""
        className={cn(
          "mx-auto w-full max-w-col px-safe-4 pt-2 xs:px-safe-5",
          // Нижняя панель (≈ 65 px + safe area) закрывает низ экрана на телефоне; на ноутбуке её нет.
          keyboardOpen ? "pb-safe-2" : "pb-[calc(4.25rem+env(safe-area-inset-bottom,0px))] lg:pb-safe-4",
        )}
      >
        {composer}
      </div>
    </div>
  );
}

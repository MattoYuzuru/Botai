import { type ReactNode, useEffect, useState } from "react";
import { cn } from "../../lib/cn";

export interface ChatLayoutProps {
  /** Верхняя полоса чата (`ChatTopBar`): на всю ширину области, высота `h-topbar`. */
  header: ReactNode;
  /** Сообщения: страница прокручивается целиком, колонка `max-w-chat` по центру. */
  children: ReactNode;
  /** Поле ввода (`ChatComposer`) вместе со слоем Вари. */
  composer: ReactNode;
  /** Открыта экранная клавиатура: нижняя панель скрыта, поле прилипает к клавиатуре (styleguide §7). */
  keyboardOpen?: boolean;
  /** Над полем ввода по центру: кнопка «Вниз». */
  floating?: ReactNode;
  className?: string;
}

/**
 * Рамка экрана чата на весь экран (styleguide §7): верхняя полоса липнет к верху, поле ввода — к низу, а между
 * ними прокручивается сама страница, поэтому скроллбар стоит у правого края окна, а не внутри колонки. Колонка
 * сообщений и поля — `max-w-chat` (48rem) по центру. На ноутбуке поле ввода отступает от низа на 20 px; на
 * телефоне оно стоит над нижней панелью и safe area, а при открытой клавиатуре — прямо над ней
 * (`interactive-widget=resizes-content` сжимает окно, и липкий низ едет вместе с клавиатурой).
 */
export function ChatLayout({
  header,
  children,
  composer,
  keyboardOpen = false,
  floating,
  className,
}: ChatLayoutProps) {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div
      data-chat-layout=""
      className={cn(
        "flex min-h-0 flex-1 flex-col transition-[padding-bottom] duration-slow ease-soft",
        // Место под нижнюю панель: без него поле ввода, прилипшее над ней, налезало бы на последнее сообщение.
        !keyboardOpen && "pb-[calc(4.0625rem+env(safe-area-inset-bottom,0px))] lg:pb-0",
        className,
      )}
    >
      <div
        data-chat-header=""
        data-scrolled={scrolled || undefined}
        className="sticky top-0 z-sticky border-b border-transparent bg-bg pt-safe transition-[border-color] duration-base ease-soft data-[scrolled]:border-border"
      >
        {header}
      </div>
      <div
        data-chat-transcript=""
        className="mx-auto flex w-full max-w-chat flex-1 flex-col px-safe-4 xs:px-safe-5"
      >
        {children}
      </div>
      <div
        data-chat-dock=""
        className={cn(
          "sticky z-dock bg-bg transition-[bottom,padding-bottom] duration-slow ease-soft",
          // Нижняя панель (65 px + safe area) закрывает низ экрана на телефоне: поле встаёт над ней, а не под неё.
          // На ноутбуке панели снизу нет, поле отступает от низа на 20 px.
          keyboardOpen
            ? "bottom-0 pb-safe-2"
            : "bottom-[calc(4.0625rem+env(safe-area-inset-bottom,0px))] pb-2 lg:bottom-0 lg:pb-safe-5",
        )}
      >
        <div className="relative mx-auto w-full max-w-chat px-safe-4 pt-2 xs:px-safe-5">
          {floating && (
            <div className="pointer-events-none absolute inset-x-0 bottom-full z-dock mb-2 flex justify-center">
              <div className="pointer-events-auto">{floating}</div>
            </div>
          )}
          {composer}
        </div>
      </div>
    </div>
  );
}

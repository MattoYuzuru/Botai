import { type ReactNode, useEffect, useState } from "react";
import { cn } from "../../lib/cn";
import { IconButton } from "../icon-button/icon-button";

export interface ScreenHeaderProps {
  /** Заголовок экрана — единственный `h1`. */
  title: string;
  /** `back` — стрелка «Назад» (системный жест «назад» тоже работает), либо свой элемент. */
  leading?: "back" | ReactNode;
  onBack?: () => void;
  /** Переключатель пространства, неразрушающее действие. Верхние 25 % экрана. */
  trailing?: ReactNode;
  /** Свежесть данных: справа на ноутбуке, отдельная строка на телефоне. */
  meta?: ReactNode;
  /** Подзаголовок: вуз, дата. */
  subtitle?: ReactNode;
  sticky?: boolean;
  /** Колонка чата: шапка не шире колонки 46rem и на ноутбуке (по умолчанию — на всю ширину страницы). */
  narrow?: boolean;
  className?: string;
}

/** Липкая шапка экрана. Под вырезом — safe area; граница появляется, когда экран прокручен. */
export function ScreenHeader({
  title,
  leading,
  onBack,
  trailing,
  meta,
  subtitle,
  sticky = true,
  narrow = false,
  className,
}: ScreenHeaderProps) {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    if (!sticky) return;
    const onScroll = () => setScrolled(window.scrollY > 4);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [sticky]);

  return (
    <header
      data-scrolled={scrolled || undefined}
      className={cn(
        "z-sticky border-b border-transparent bg-bg pt-safe transition-[border-color] duration-base ease-soft",
        sticky && "sticky top-0",
        "data-[scrolled]:border-border",
        className,
      )}
    >
      <div
        className={cn(
          "mx-auto flex min-h-14 w-full max-w-col flex-wrap items-center gap-x-3 gap-y-2 px-safe-4 py-5 xs:px-safe-5",
          !narrow && "lg:max-w-page",
        )}
      >
        {leading === "back" ? (
          <IconButton icon="back" label="Назад" onClick={onBack} className="-ml-2" />
        ) : (
          leading
        )}
        <div className="flex min-w-0 flex-1 basis-auto flex-col">
          <h1 className="truncate type-title text-fg">{title}</h1>
          {subtitle && <div className="truncate type-caption text-fg-muted">{subtitle}</div>}
        </div>
        {meta && (
          <div className="order-last w-full min-w-0 lg:order-none lg:w-auto lg:max-w-reading">{meta}</div>
        )}
        {trailing && (
          <div className="flex max-w-full min-w-0 shrink-0 flex-wrap items-center gap-1">{trailing}</div>
        )}
      </div>
    </header>
  );
}

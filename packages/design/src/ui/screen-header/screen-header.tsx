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
  /** Подзаголовок: вуз, дата. */
  subtitle?: ReactNode;
  sticky?: boolean;
  className?: string;
}

/** Липкая шапка экрана. Под вырезом — safe area; граница появляется, когда экран прокручен. */
export function ScreenHeader({
  title,
  leading,
  onBack,
  trailing,
  subtitle,
  sticky = true,
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
        "z-sticky border-b border-transparent bg-bg px-safe-4 pt-safe transition-[border-color] duration-base ease-soft xs:px-safe-5",
        sticky && "sticky top-0",
        "data-[scrolled]:border-border",
        className,
      )}
    >
      <div className="flex min-h-14 items-center gap-2">
        {leading === "back" ? (
          <IconButton icon="back" label="Назад" onClick={onBack} className="-ml-2" />
        ) : (
          leading
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <h1 className="truncate type-title text-fg">{title}</h1>
          {subtitle && <div className="truncate type-caption text-fg-muted">{subtitle}</div>}
        </div>
        {trailing && <div className="flex shrink-0 items-center gap-1">{trailing}</div>}
      </div>
    </header>
  );
}

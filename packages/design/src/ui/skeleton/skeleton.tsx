import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";

export interface SkeletonProps extends ComponentProps<"div"> {
  /** Ширина блока: доля («60%») или любая CSS-длина. По умолчанию — на всю ширину. */
  width?: string;
}

/**
 * Форма будущего содержимого. Блик спокойный; при сокращённом движении — ровная заливка.
 * Контейнер помечается `aria-busy` — для области данных это единственный индикатор загрузки.
 */
export function Skeleton({ width, className, style, ...props }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={cn("skeleton h-4 rounded-md", className)}
      style={width ? { width, ...style } : style}
      {...props}
    />
  );
}

export interface SkeletonTextProps extends ComponentProps<"div"> {
  lines?: number;
  /** Подпись для скринридера. */
  label?: string;
}

/** Абзац из строк; последняя короче. */
export function SkeletonText({ lines = 3, label = "Загрузка", className, ...props }: SkeletonTextProps) {
  return (
    <div aria-busy="true" role="status" className={cn("flex flex-col gap-2", className)} {...props}>
      <span className="sr-only">{label}</span>
      {Array.from({ length: lines }, (_, index) => (
        // Строки статичны и не переупорядочиваются.
        // biome-ignore lint/suspicious/noArrayIndexKey: порядок строк постоянен
        <Skeleton key={index} width={index === lines - 1 && lines > 1 ? "60%" : undefined} />
      ))}
    </div>
  );
}

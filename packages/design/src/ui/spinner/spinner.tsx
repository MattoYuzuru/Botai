import { cn } from "../../lib/cn";

/**
 * Индикатор внутри кнопки — единственное место, где разрешён спиннер (styleguide §8, §9).
 * Для областей данных есть только скелетон. Цвет — `currentColor`.
 */
export function Spinner({ size = 20, className }: { size?: 16 | 20; className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-spinner=""
      className={cn(
        "inline-block animate-spin rounded-full border-2 border-current border-t-transparent",
        size === 16 ? "size-4" : "size-5",
        className,
      )}
    />
  );
}

import { ScrollArea as ScrollAreaPrimitive } from "radix-ui";
import type { ComponentProps, Ref, UIEventHandler } from "react";
import { cn } from "../../lib/cn";

export interface ScrollAreaProps extends ComponentProps<typeof ScrollAreaPrimitive.Root> {
  /** Класс вьюпорта (отступы содержимого). */
  viewportClassName?: string;
  viewportRef?: Ref<HTMLDivElement>;
  onViewportScroll?: UIEventHandler<HTMLDivElement>;
  orientation?: "vertical" | "horizontal";
}

/**
 * Единственная разрешённая прокручиваемая область внутри экрана (design-lint: raw-scroll); сама страница
 * прокручивается документом с тем же тонким бегунком (`base.css`). Нативная прокрутка и инерция сохраняются.
 * Бегунок из токенов `--scrollbar-*`: тонкий, у самого края, без дорожки и рамок; по умолчанию появляется
 * при наведении и прокрутке (`type="hover"`).
 */
export function ScrollArea({
  className,
  viewportClassName,
  viewportRef,
  onViewportScroll,
  orientation = "vertical",
  type = "hover",
  children,
  ...props
}: ScrollAreaProps) {
  return (
    <ScrollAreaPrimitive.Root type={type} className={cn("relative overflow-hidden", className)} {...props}>
      <ScrollAreaPrimitive.Viewport
        ref={viewportRef}
        onScroll={onViewportScroll}
        className={cn("size-full overscroll-contain rounded-[inherit]", viewportClassName)}
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollAreaPrimitive.Scrollbar
        orientation={orientation}
        data-scrollbar={orientation}
        className={cn(
          "flex touch-none bg-(--scrollbar-track) select-none p-0 transition-opacity duration-base",
          orientation === "vertical" ? "h-full w-(--scrollbar-size)" : "h-(--scrollbar-size) flex-col",
        )}
      >
        <ScrollAreaPrimitive.Thumb className="relative flex-1 rounded-full border-2 border-transparent bg-(--scrollbar-thumb) bg-clip-padding hover:bg-(--scrollbar-thumb-hover)" />
      </ScrollAreaPrimitive.Scrollbar>
    </ScrollAreaPrimitive.Root>
  );
}

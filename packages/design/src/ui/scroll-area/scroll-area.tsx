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
 * Единственная разрешённая прокручиваемая область (design-lint: raw-scroll). Нативная прокрутка
 * и инерция сохраняются, полоса тонкая и появляется при наведении.
 */
export function ScrollArea({
  className,
  viewportClassName,
  viewportRef,
  onViewportScroll,
  orientation = "vertical",
  children,
  ...props
}: ScrollAreaProps) {
  return (
    <ScrollAreaPrimitive.Root className={cn("relative overflow-hidden", className)} {...props}>
      <ScrollAreaPrimitive.Viewport
        ref={viewportRef}
        onScroll={onViewportScroll}
        className={cn("size-full overscroll-contain rounded-[inherit]", viewportClassName)}
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollAreaPrimitive.Scrollbar
        orientation={orientation}
        className={cn(
          "flex touch-none select-none p-0.5 transition-opacity duration-base",
          orientation === "vertical" ? "h-full w-2.5" : "h-2.5 flex-col",
        )}
      >
        <ScrollAreaPrimitive.Thumb className="relative flex-1 rounded-full bg-border-strong hover:bg-control" />
      </ScrollAreaPrimitive.Scrollbar>
    </ScrollAreaPrimitive.Root>
  );
}

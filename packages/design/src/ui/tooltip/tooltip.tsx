import { Tooltip as TooltipPrimitive } from "radix-ui";
import type { ReactElement, ReactNode } from "react";

export interface TooltipProps {
  content: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  children: ReactElement;
}

/** Подсказка для мыши и клавиатуры. На касание не рассчитана: смысл всегда есть и текстом. */
export function Tooltip({ content, side = "top", children }: TooltipProps) {
  return (
    <TooltipPrimitive.Provider delayDuration={400} skipDelayDuration={200}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content
            side={side}
            sideOffset={6}
            collisionPadding={8}
            className="z-layer rounded-md bg-fg px-2 py-1 type-caption text-bg shadow-md motion-pop"
          >
            {content}
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  );
}

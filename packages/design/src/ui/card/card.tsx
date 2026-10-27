import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";

const cardVariants = cva("block rounded-lg text-fg", {
  variants: {
    variant: {
      outline: "border border-border bg-surface",
      elevated: "border border-border bg-surface shadow-sm",
      sunken: "bg-bg-subtle",
    },
    padding: { none: "p-0", sm: "p-3", md: "p-4", lg: "p-5" },
    interactive: {
      true: "cursor-pointer text-left transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed",
      false: "",
    },
  },
  defaultVariants: { variant: "outline", padding: "md", interactive: false },
});

export interface CardProps extends ComponentProps<"div">, VariantProps<typeof cardVariants> {
  /** Отрисовать дочерний элемент (ссылку или кнопку) со стилями карточки. */
  asChild?: boolean;
}

/**
 * Группа связанного содержимого. Карточки не вкладываем друг в друга. Карточка-кнопка опознаётся
 * содержимым (стрелка, иконка действия), а не рамкой.
 */
export function Card({ variant, padding, interactive, asChild, className, ...props }: CardProps) {
  const Component = asChild ? Slot.Root : "div";
  return <Component className={cn(cardVariants({ variant, padding, interactive }), className)} {...props} />;
}

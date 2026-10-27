import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/cn";

const wordmark = cva("inline-flex font-semibold whitespace-nowrap text-fg", {
  variants: {
    size: {
      sm: "type-subheading",
      md: "type-heading",
      lg: "type-display",
    },
  },
  defaultVariants: { size: "md" },
});

export interface WordmarkProps extends VariantProps<typeof wordmark> {
  className?: string;
}

/**
 * Словесный знак BotAI (styleguide §1.1): «Bot» — `fg`, «AI» — `accent-fg`, тот же кегль и вес,
 * без пробела. В пространстве вуза «AI» берёт акцент вуза — единственное место, где цвет вуза попадает в бренд.
 */
export function Wordmark({ size, className }: WordmarkProps) {
  return (
    <span role="img" aria-label="BotAI" className={cn(wordmark({ size }), className)}>
      <span aria-hidden="true">Bot</span>
      <span aria-hidden="true" className="text-accent-fg">
        AI
      </span>
    </span>
  );
}

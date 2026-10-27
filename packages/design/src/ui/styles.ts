/**
 * Общие классы дизайн-системы: рамка полей, поверхность всплывающих слоёв, строка выбора.
 * Используются только внутри ui — так поля, шиты и меню выглядят одинаково.
 */
import { cva } from "class-variance-authority";

/**
 * Рамка поля (Input, Textarea, Select): заливка `bg-subtle` и подпись опознают поле, рамка
 * декоративна. При наведении рамка становится `control`, при фокусе — `focus-ring` (styleguide §2).
 * Кегль поля — 16 px (`type-body`): меньше нельзя, iOS Safari приблизит страницу при фокусе.
 */
export const controlFrame = cva(
  [
    "flex w-full items-center gap-2 rounded-md border border-border-strong bg-bg-subtle px-3 text-fg type-body",
    "transition-[border-color,background-color] duration-base ease-soft",
    "hover:border-control",
    "focus-within:border-focus-ring focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus-ring focus-within:hover:border-focus-ring",
    "data-[invalid]:border-danger-fg data-[invalid]:hover:border-danger-fg",
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-60",
  ],
  {
    variants: { multiline: { true: "items-start py-2.5", false: "h-11" } },
    defaultVariants: { multiline: false },
  },
);

/** Поверхность поповера, меню и подсказки. */
export const floatingSurface =
  "z-layer border border-border bg-surface-raised text-fg shadow-md outline-none motion-pop";

/** Строка выбора (флажок, радио, пункт списка): вся строка — зона нажатия не меньше 44 px. */
export const choiceRow =
  "group/row flex min-h-11 w-full cursor-pointer items-start gap-3 rounded-md px-3 py-2.5 text-left transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed data-[disabled]:cursor-not-allowed data-[disabled]:opacity-60";

/** Ссылка в тексте: цвет — только `accent-fg`. */
export const linkClass =
  "text-accent-fg underline decoration-from-font underline-offset-2 hover:decoration-2 rounded-xs";

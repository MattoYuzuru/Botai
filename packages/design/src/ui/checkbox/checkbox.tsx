import { Checkbox as CheckboxPrimitive } from "radix-ui";
import { type ComponentProps, type ReactNode, useId } from "react";
import { cn } from "../../lib/cn";
import { useFieldControl } from "../field/field";
import { choiceRow } from "../styles";

export interface CheckboxProps extends Omit<ComponentProps<typeof CheckboxPrimitive.Root>, "children"> {
  /** Подпись. Вся строка — зона нажатия (не меньше 44 px). */
  label: ReactNode;
  /** Вторая строка под подписью, если подпись сама не объясняет выбор. */
  description?: ReactNode;
  invalid?: boolean;
}

/**
 * Флажок. Состояние «частично» — `checked="indeterminate"`. Рамка — `control` (≥ 3:1),
 * выбранный — заливка `accent` и белая галочка. Работает внутри `ChatMessage` (уточняющие вопросы агента).
 */
export function Checkbox({ label, description, invalid, disabled, id, className, ...props }: CheckboxProps) {
  const autoId = useId();
  const descriptionId = description ? `${autoId}-description` : undefined;
  const field = useFieldControl({ id: id ?? autoId, invalid, disabled, describedBy: descriptionId });
  const controlId = field.id ?? autoId;
  return (
    <label
      htmlFor={controlId}
      data-disabled={field.disabled || undefined}
      data-invalid={field.invalid || undefined}
      className={cn(choiceRow, className)}
    >
      <CheckboxPrimitive.Root
        id={controlId}
        disabled={field.disabled}
        aria-invalid={field["aria-invalid"]}
        aria-describedby={field["aria-describedby"]}
        className={cn(
          "mt-0.5 inline-grid size-5 shrink-0 cursor-pointer place-items-center rounded-xs border border-control bg-surface text-fg-on-accent",
          "transition-[background-color,border-color] duration-fast ease-soft",
          "data-[state=checked]:border-accent data-[state=checked]:bg-accent data-[state=indeterminate]:border-accent data-[state=indeterminate]:bg-accent",
          "group-hover/row:border-fg-muted group-data-[invalid]/row:border-danger-fg",
          "disabled:cursor-not-allowed",
        )}
        {...props}
      >
        <CheckboxPrimitive.Indicator className="grid place-items-center">
          <Mark indeterminate={props.checked === "indeterminate"} />
        </CheckboxPrimitive.Indicator>
      </CheckboxPrimitive.Root>
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="type-ui text-fg">{label}</span>
        {description && (
          <span id={descriptionId} className="type-caption text-fg-muted">
            {description}
          </span>
        )}
      </span>
    </label>
  );
}

/** Галочка и «минус» рисуются штрихом SVG: Tabler-иконка 16 px при обводке 1.5 в 20-пиксельной рамке выглядит тонкой. */
function Mark({ indeterminate }: { indeterminate: boolean }) {
  return (
    <svg viewBox="0 0 16 16" fill="none" aria-hidden="true" className="size-4">
      {indeterminate ? (
        <path d="M4 8h8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      ) : (
        <path
          d="M3.75 8.5 6.75 11.5 12.25 5"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
    </svg>
  );
}

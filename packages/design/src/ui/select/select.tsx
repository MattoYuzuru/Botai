import { Popover } from "radix-ui";
import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import { useMediaQuery } from "../../lib/hooks";
import { useFieldControl } from "../field/field";
import { Icon, type IconName } from "../icon";
import { Sheet } from "../sheet/sheet";
import { controlFrame, floatingSurface } from "../styles";

export interface SelectOption {
  value: string;
  label: string;
  icon?: IconName;
  disabled?: boolean;
}

export interface SelectProps {
  options: readonly SelectOption[];
  value?: string;
  onValueChange?: (value: string) => void;
  placeholder?: string;
  invalid?: boolean;
  disabled?: boolean;
  id?: string;
  name?: string;
  /** Заголовок шита на телефоне (по умолчанию — «Выберите»). */
  sheetTitle?: string;
  "aria-label"?: string;
  className?: string;
}

/**
 * Выбор одного значения. На телефоне список раскрывается шитом (styleguide §8), на ноутбуке — поповером.
 * Семантика listbox: стрелки, Home/End, Enter и Space выбирают, Esc закрывает, ввод буквы ищет по подписи.
 */
export function Select({
  options,
  value,
  onValueChange,
  placeholder = "Выберите",
  invalid,
  disabled,
  id,
  name,
  sheetTitle = "Выберите",
  "aria-label": ariaLabel,
  className,
}: SelectProps) {
  const [open, setOpen] = useState(false);
  const field = useFieldControl({ id, invalid, disabled });
  const listId = useId();
  const compact = !useMediaQuery("(min-width: 48rem)");
  const selected = options.find((option) => option.value === value);

  const choose = (next: string) => {
    onValueChange?.(next);
    setOpen(false);
  };

  const trigger = (
    <button
      type="button"
      id={field.id}
      role="combobox"
      aria-label={ariaLabel}
      aria-expanded={open}
      aria-haspopup="listbox"
      aria-controls={open ? listId : undefined}
      aria-invalid={field["aria-invalid"]}
      aria-describedby={field["aria-describedby"]}
      data-invalid={field.invalid || undefined}
      data-disabled={field.disabled || undefined}
      data-placeholder={selected ? undefined : ""}
      disabled={field.disabled}
      onClick={compact ? () => setOpen(true) : undefined}
      className={cn(
        controlFrame(),
        "cursor-pointer justify-between text-left",
        "data-[placeholder]:text-fg-subtle",
        className,
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        {selected?.icon && <Icon name={selected.icon} size={20} className="text-fg-muted" />}
        <span className="truncate">{selected ? selected.label : placeholder}</span>
      </span>
      <Icon name="selector" size={20} className="text-fg-subtle" />
    </button>
  );

  const list = (
    <OptionList
      id={listId}
      options={options}
      value={value}
      onChoose={choose}
      onClose={() => setOpen(false)}
    />
  );

  return (
    <>
      {name && <input type="hidden" name={name} value={value ?? ""} />}
      {compact ? (
        <>
          {trigger}
          <Sheet open={open} onOpenChange={setOpen} title={sheetTitle} size="auto">
            {list}
          </Sheet>
        </>
      ) : (
        <Popover.Root open={open} onOpenChange={setOpen}>
          <Popover.Trigger asChild>{trigger}</Popover.Trigger>
          <Popover.Portal>
            <Popover.Content
              sideOffset={6}
              align="start"
              collisionPadding={8}
              onOpenAutoFocus={(event) => {
                // Фокус уходит в список, а не на первый элемент поповера.
                event.preventDefault();
                (event.currentTarget as HTMLElement).querySelector<HTMLElement>("[role=listbox]")?.focus();
              }}
              className={cn(floatingSurface, "w-(--radix-popover-trigger-width) rounded-md p-1")}
            >
              {list}
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
      )}
    </>
  );
}

interface OptionListProps {
  id: string;
  options: readonly SelectOption[];
  value?: string;
  onChoose: (value: string) => void;
  onClose: () => void;
}

function OptionList({ id, options, value, onChoose, onClose }: OptionListProps) {
  const enabled = options
    .map((option, index) => (option.disabled ? -1 : index))
    .filter((index) => index >= 0);
  const [active, setActive] = useState(() => {
    const current = options.findIndex((option) => option.value === value && !option.disabled);
    return current >= 0 ? current : (enabled[0] ?? 0);
  });
  const typed = useRef({ text: "", at: 0 });
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Шит не знает про наш фокус: ставим его на список сами.
    listRef.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const move = (delta: number) => {
    const position = enabled.indexOf(active);
    const next = enabled[(position + delta + enabled.length) % enabled.length];
    if (next !== undefined) setActive(next);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        move(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        move(-1);
        return;
      case "Home":
        event.preventDefault();
        if (enabled[0] !== undefined) setActive(enabled[0]);
        return;
      case "End":
        event.preventDefault();
        if (enabled.length) setActive(enabled[enabled.length - 1] as number);
        return;
      case "Enter":
      case " ": {
        event.preventDefault();
        const option = options[active];
        if (option && !option.disabled) onChoose(option.value);
        return;
      }
      case "Escape":
        event.preventDefault();
        onClose();
        return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
      const now = event.timeStamp;
      typed.current.text = now - typed.current.at > 700 ? event.key : typed.current.text + event.key;
      typed.current.at = now;
      const query = typed.current.text.toLowerCase();
      const hit = options.findIndex(
        (option) => !option.disabled && option.label.toLowerCase().startsWith(query),
      );
      if (hit >= 0) setActive(hit);
    }
  };

  return (
    <div
      ref={listRef}
      id={id}
      role="listbox"
      tabIndex={-1}
      aria-activedescendant={`${id}-${active}`}
      onKeyDown={onKeyDown}
      className="flex flex-col gap-0.5 outline-none"
    >
      {options.map((option, index) => (
        <OptionRow
          key={option.value}
          id={`${id}-${index}`}
          index={index}
          option={option}
          selected={option.value === value}
          active={index === active}
          onChoose={() => onChoose(option.value)}
          onHover={() => !option.disabled && setActive(index)}
        />
      ))}
    </div>
  );
}

function OptionRow({
  id,
  index,
  option,
  selected,
  active,
  onChoose,
  onHover,
}: {
  id: string;
  index: number;
  option: SelectOption;
  selected: boolean;
  active: boolean;
  onChoose: () => void;
  onHover: () => void;
}): ReactNode {
  return (
    // Клавиатура обрабатывается на listbox через aria-activedescendant, поэтому у строки нет своего обработчика.
    // biome-ignore lint/a11y/useKeyWithClickEvents: клавиатура обрабатывается на listbox
    <div
      id={id}
      role="option"
      tabIndex={-1}
      data-index={index}
      aria-selected={selected}
      aria-disabled={option.disabled || undefined}
      data-active={active || undefined}
      onClick={option.disabled ? undefined : onChoose}
      onPointerMove={onHover}
      className={cn(
        "flex min-h-11 cursor-pointer items-center gap-3 rounded-md px-3 type-ui text-fg",
        "data-[active]:bg-hover aria-disabled:cursor-not-allowed aria-disabled:opacity-45",
      )}
    >
      {option.icon && <Icon name={option.icon} size={20} className="text-fg-muted" />}
      <span className="min-w-0 flex-1 truncate">{option.label}</span>
      {selected && <Icon name="check" size={20} className="text-accent-fg" />}
    </div>
  );
}

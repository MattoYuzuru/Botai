import { useRef } from "react";
import { cn } from "../../lib/cn";
import { NBSP } from "../../lib/format";
import { Button } from "../button/button";
import { useFieldControl } from "../field/field";
import { Icon } from "../icon";
import { IconButton } from "../icon-button/icon-button";

/** «1,2 МБ», «340 КБ»: размер файла для человека; единицы отделены неразрывным пробелом. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}${NBSP}Б`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}${NBSP}КБ`;
  return `${(bytes / (1024 * 1024)).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}${NBSP}МБ`;
}

export interface FilePickerProps {
  value: File | null;
  onValueChange: (file: File | null) => void;
  /** Допустимые расширения для диалога выбора: «.pdf,.docx». Проверку размера и типа делает вызывающий код. */
  accept?: string;
  /** Подпись кнопки, пока файл не выбран. */
  label?: string;
  invalid?: boolean;
  disabled?: boolean;
  id?: string;
  className?: string;
}

/**
 * Выбор файла: кнопка вместо нативного поля. Выбранный файл — имя и размер, «Убрать файл» снимает выбор.
 * Нативный `<input type="file">` спрятан и недоступен с клавиатуры отдельно: фокус и подпись у кнопки.
 */
export function FilePicker({
  value,
  onValueChange,
  accept,
  label = "Выбрать файл",
  invalid,
  disabled,
  id,
  className,
}: FilePickerProps) {
  const input = useRef<HTMLInputElement>(null);
  const field = useFieldControl({ id, invalid, disabled });
  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <input
        ref={input}
        type="file"
        accept={accept}
        tabIndex={-1}
        aria-hidden="true"
        data-file-input=""
        className="sr-only"
        onChange={(event) => {
          onValueChange(event.target.files?.[0] ?? null);
          // Тот же файл можно выбрать повторно после «Убрать файл».
          event.target.value = "";
        }}
      />
      {value ? (
        <div
          data-invalid={field.invalid || undefined}
          className="flex min-h-11 items-center gap-3 rounded-md border border-border-strong bg-bg-subtle px-3 data-[invalid]:border-danger-fg"
        >
          <Icon name="document" size={20} className="shrink-0 text-fg-subtle" />
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="break-words type-ui text-fg">{value.name}</span>
            <span className="type-caption text-fg-muted" data-numeric="">
              {formatBytes(value.size)}
            </span>
          </span>
          <IconButton
            icon="close"
            label="Убрать файл"
            size="sm"
            disabled={field.disabled}
            onClick={() => onValueChange(null)}
          />
        </div>
      ) : (
        <Button
          id={field.id}
          variant="secondary"
          leadingIcon="plus"
          disabled={field.disabled}
          aria-describedby={field["aria-describedby"]}
          aria-invalid={field["aria-invalid"]}
          onClick={() => input.current?.click()}
        >
          {label}
        </Button>
      )}
    </div>
  );
}

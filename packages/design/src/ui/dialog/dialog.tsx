import { AlertDialog } from "radix-ui";
import { type ReactNode, useRef, useState } from "react";
import { Button } from "../button/button";

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Вопрос в заголовке: «Отключить ЛК ВШЭ?». */
  title: string;
  /** Последствия: что произойдёт с данными. */
  description: ReactNode;
  /** Кнопка-глагол: «Отключить», «Удалить». Не «ОК» и не «Да». */
  confirmLabel: string;
  cancelLabel?: string;
  onConfirm: () => unknown;
  tone?: "default" | "danger";
}

/**
 * Подтверждение необратимого (styleguide §9). Всё остальное — шит. Браузерные confirm/alert запрещены.
 * Фокус сначала на «Отмена»: случайный Enter ничего не удаляет.
 */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel = "Отмена",
  onConfirm,
  tone = "danger",
}: DialogProps) {
  const [busy, setBusy] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  const confirm = async () => {
    setBusy(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <AlertDialog.Root open={open} onOpenChange={onOpenChange}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="motion-overlay fixed inset-0 z-scrim bg-overlay" />
        <AlertDialog.Content
          onOpenAutoFocus={() => {
            opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            opener.current?.focus();
          }}
          className="motion-dialog fixed top-1/2 left-1/2 z-layer flex w-[min(24rem,calc(100%-2rem))] -translate-x-1/2 -translate-y-1/2 flex-col gap-4 rounded-xl border border-border bg-surface-raised p-5 text-fg shadow-lg outline-none"
        >
          <div className="flex flex-col gap-2">
            <AlertDialog.Title className="type-heading text-fg">{title}</AlertDialog.Title>
            <AlertDialog.Description className="type-ui text-fg-muted">{description}</AlertDialog.Description>
          </div>
          <div className="flex flex-col-reverse gap-2 xs:flex-row xs:flex-wrap xs:justify-end">
            <AlertDialog.Cancel asChild>
              <Button variant="secondary">{cancelLabel}</Button>
            </AlertDialog.Cancel>
            <Button
              variant="primary"
              tone={tone}
              loading={busy}
              onClick={(event) => {
                event.preventDefault();
                void confirm();
              }}
            >
              {confirmLabel}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

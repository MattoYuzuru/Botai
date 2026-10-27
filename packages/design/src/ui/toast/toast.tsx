import type { ReactNode } from "react";
import { Toaster as SonnerToaster, toast as sonnerToast } from "sonner";
import { cn } from "../../lib/cn";
import { Icon } from "../icon";

/**
 * Ставится один раз в корне приложения. На телефоне уведомление встаёт над нижней панелью
 * и полем ввода, на ноутбуке — справа снизу. Не больше трёх сразу.
 */
export function Toaster() {
  return (
    <SonnerToaster
      position="bottom-center"
      gap={8}
      visibleToasts={3}
      duration={5000}
      containerAriaLabel="Уведомления"
      offset={{ bottom: 24, right: 24 }}
      mobileOffset={{ bottom: "calc(5.5rem + env(safe-area-inset-bottom, 0px))", left: 16, right: 16 }}
      icons={{
        success: <Icon name="ok" className="text-success" />,
        error: <Icon name="error" className="text-danger-fg" />,
        warning: <Icon name="warning" className="text-warning" />,
        info: <Icon name="info" className="text-info" />,
      }}
      toastOptions={{
        unstyled: true,
        classNames: {
          toast: cn(
            "pointer-events-auto flex w-full items-start gap-3 rounded-lg border border-border bg-surface-raised p-3 text-fg shadow-lg xs:w-[22rem]",
          ),
          icon: "mt-0.5 flex size-5 shrink-0 items-center justify-center",
          content: "flex min-w-0 flex-1 flex-col gap-0.5",
          title: "type-ui font-medium text-fg",
          description: "type-caption text-fg-muted",
          actionButton:
            "inline-flex h-9 shrink-0 cursor-pointer items-center rounded-md px-3 type-ui font-medium text-accent-fg hover:bg-hover",
        },
      }}
    />
  );
}

export type ToastTone = "default" | "success" | "error" | "warning" | "info";

export interface ToastOptions {
  /** Одна фраза-уточнение: что дальше. Не повторяет заголовок. */
  description?: ReactNode;
  /** Одно действие: «Отменить». */
  action?: { label: string; onClick: () => void };
  tone?: ToastTone;
  duration?: number;
  /** Повторный вызов с тем же id обновляет уведомление. */
  id?: string | number;
}

/**
 * Результат действия, в прошедшем времени: «Вуз подключён». Для сбоя — что не получилось и что будет
 * дальше («Отправим, когда вуз ответит»). Кнопки «Повторить» у уведомления нет.
 */
export function toast(title: string, options: ToastOptions = {}) {
  const { tone = "default", action, description, duration, id } = options;
  const payload = {
    description,
    duration,
    id,
    action: action ? { label: action.label, onClick: () => action.onClick() } : undefined,
  };
  switch (tone) {
    case "success":
      return sonnerToast.success(title, payload);
    case "error":
      return sonnerToast.error(title, payload);
    case "warning":
      return sonnerToast.warning(title, payload);
    case "info":
      return sonnerToast.info(title, payload);
    default:
      return sonnerToast(title, payload);
  }
}

toast.dismiss = (id?: string | number) => sonnerToast.dismiss(id);

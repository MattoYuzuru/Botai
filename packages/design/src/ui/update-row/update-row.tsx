import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";
import { listRowClass } from "../list-row/list-row";

export interface UpdateRowProps {
  /** Что изменилось: новая оценка или статус задания. */
  kind: "grade" | "deadline";
  /** Работа или задание: «Контрольная №2». */
  title: string;
  /** Вторая строка: предмет, когда изменилось, вуз. */
  meta?: ReactNode;
  /** Новая оценка словами: «8 из 10». Показывается крупно справа. */
  value?: string | null;
  /** Новый статус задания: `Badge` со словами источника. */
  status?: ReactNode;
  /** Задание сдано или проверено: иконка «готово» вместо значка задания. */
  done?: boolean;
  onOpen?: () => void;
  className?: string;
}

/**
 * Строка «Нового» (D-044, D-046): что изменилось в источнике — оценка («Контрольная №2 · 8 из 10») или статус
 * задания («Решение прикреплено»). Значок вида слева на подложке, статус — меткой во второй строке перед
 * предметом, значение — справа табличными цифрами. Название — не длиннее двух строк. Цвет не
 * оценивает оценку: значок оценки — акцент, задания — нейтральный, сданное — «готово». С `onOpen` — кнопка ≥ 56 px.
 */
export function UpdateRow({
  kind,
  title,
  meta,
  value,
  status,
  done = false,
  onOpen,
  className,
}: UpdateRowProps) {
  const icon: IconName = kind === "grade" ? "grade" : done ? "ok" : "deadline";
  const body = (
    <>
      <span
        aria-hidden="true"
        className={cn(
          "flex size-9 shrink-0 items-center justify-center rounded-md",
          kind === "grade"
            ? "bg-accent-subtle text-accent-fg"
            : done
              ? "bg-success-subtle text-success-fg"
              : "bg-pressed text-fg-muted",
        )}
      >
        <Icon name={icon} size={20} />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="line-clamp-2 break-words type-subheading text-fg">{title}</span>
        {(status || meta) && (
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">
            {status}
            {meta}
          </span>
        )}
      </span>
      {value && (
        <span className="max-w-[40%] shrink-0 break-words text-right type-subheading text-fg" data-numeric="">
          {value}
        </span>
      )}
    </>
  );
  if (!onOpen) {
    return (
      <div
        data-update-row={kind}
        className={cn(listRowClass, "cursor-default hover:bg-transparent active:bg-transparent", className)}
      >
        {body}
      </div>
    );
  }
  return (
    <button type="button" data-update-row={kind} onClick={onOpen} className={cn(listRowClass, className)}>
      {body}
    </button>
  );
}

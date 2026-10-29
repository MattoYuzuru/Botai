import { cn } from "../../lib/cn";
import { NBSP } from "../../lib/format";
import { attachmentIcon, type ComposerAttachment } from "../attachments";
import { formatBytes } from "../file-picker/file-picker";
import { Icon } from "../icon";
import { IconButton } from "../icon-button/icon-button";

export interface AttachmentChipProps {
  attachment: ComposerAttachment;
  /** «Убрать»: работает в любом состоянии, в том числе пока файл грузится. */
  onRemove?: (id: string) => void;
  /** Нажатие на тело чипа открывает вьюер; пока файл грузится, оно неактивно. */
  onOpen?: (id: string) => void;
  className?: string;
}

/**
 * Чип вложения в поле ввода: миниатюра или иконка вида, имя, размер. Пока файл грузится — тонкая полоса
 * (с процентом, только если он известен серверу или транспорту); не удалось — текст ошибки и по-прежнему «Убрать».
 */
export function AttachmentChip({ attachment, onRemove, onOpen, className }: AttachmentChipProps) {
  const { id, name, kind, size, status, progress, previewUrl, error } = attachment;
  const uploading = status === "uploading";
  const failed = status === "failed";
  const known = typeof progress === "number" && Number.isFinite(progress);
  const percent = known ? Math.round(Math.min(1, Math.max(0, progress)) * 100) : null;
  const openable = Boolean(onOpen) && !uploading;
  const caption = uploading
    ? percent === null
      ? "Загрузка…"
      : `Загрузка${NBSP}${percent}${NBSP}%`
    : failed
      ? (error ?? "Не удалось загрузить")
      : formatBytes(size);

  const body = (
    <>
      {previewUrl && kind === "image" ? (
        <img src={previewUrl} alt="" className="size-10 shrink-0 rounded-sm object-cover" />
      ) : (
        <span
          className={cn(
            "grid size-10 shrink-0 place-items-center rounded-sm bg-pressed",
            failed ? "text-danger-fg" : "text-fg-muted",
          )}
        >
          <Icon name={failed ? "warning" : attachmentIcon(kind)} size={20} />
        </span>
      )}
      <span className="flex min-w-0 flex-col">
        <span className="truncate type-label text-fg">{name}</span>
        <span
          className={cn("truncate type-caption", failed ? "text-danger-fg" : "text-fg-muted")}
          data-numeric=""
        >
          {caption}
        </span>
      </span>
    </>
  );

  return (
    <div
      data-attachment-chip=""
      data-status={status}
      className={cn(
        "relative flex min-h-12 max-w-full min-w-0 items-center gap-1 overflow-hidden rounded-md border bg-bg-subtle py-1 pl-1",
        failed ? "border-danger-fg" : "border-border",
        className,
      )}
    >
      {openable ? (
        <button
          type="button"
          aria-label={`Открыть ${name}`}
          onClick={() => onOpen?.(id)}
          className="flex min-w-0 max-w-56 cursor-pointer items-center gap-2 rounded-sm text-left transition-colors duration-fast ease-soft hover:bg-hover"
        >
          {body}
        </button>
      ) : (
        <div className="flex min-w-0 max-w-56 items-center gap-2">{body}</div>
      )}
      {onRemove && (
        <IconButton
          icon="close"
          label={`Убрать ${name}`}
          size="sm"
          onClick={() => onRemove(id)}
          className="shrink-0"
        />
      )}
      {uploading && (
        <span
          role="progressbar"
          aria-label={`Загрузка ${name}`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent ?? undefined}
          className="absolute inset-x-0 bottom-0 h-0.5 bg-border"
        >
          <span
            className={cn("block h-full bg-accent", percent === null && "w-1/3 animate-pulse-soft")}
            style={percent === null ? undefined : { width: `${percent}%` }}
          />
        </span>
      )}
    </div>
  );
}

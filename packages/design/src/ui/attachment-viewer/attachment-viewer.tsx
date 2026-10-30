import { Dialog as DialogPrimitive } from "radix-ui";
import type { KeyboardEvent, ReactNode } from "react";
import { cn } from "../../lib/cn";
import { attachmentIcon, attachmentKindLabel, type ViewerAttachment } from "../attachments";
import { buttonVariants } from "../button/button";
import { formatBytes } from "../file-picker/file-picker";
import { Icon } from "../icon";
import { IconButton } from "../icon-button/icon-button";

export interface AttachmentViewerProps {
  items: readonly ViewerAttachment[];
  /** Какое вложение открыто; `null` — вьюер закрыт. */
  openId: string | null;
  /** Закрытие и листание: новый `id` или `null`. */
  onOpenChange: (id: string | null) => void;
}

const isPdf = (item: ViewerAttachment) => item.mime === "application/pdf";
const isImage = (item: ViewerAttachment) => item.kind === "image" || item.mime.startsWith("image/");
const isAudio = (item: ViewerAttachment) => item.kind === "audio" || item.mime.startsWith("audio/");

function IconLink({
  href,
  label,
  icon,
  download,
}: {
  href: string;
  label: string;
  icon: "download" | "external";
  download?: string;
}) {
  return (
    <a
      href={href}
      aria-label={label}
      {...(download !== undefined ? { download } : { target: "_blank", rel: "noopener noreferrer" })}
      className={cn(buttonVariants({ variant: "ghost" }), "size-11 px-0")}
    >
      <Icon name={icon} size={20} />
    </a>
  );
}

function Body({ item }: { item: ViewerAttachment }): ReactNode {
  if (isImage(item)) {
    return <img src={item.url} alt={item.name} className="max-h-full max-w-full object-contain" />;
  }
  if (isPdf(item)) {
    return (
      <div className="flex size-full flex-col gap-2">
        <iframe
          src={item.url}
          title={item.name}
          className="min-h-0 w-full flex-1 rounded-md border-0 bg-surface"
        />
        <a
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          className="self-center type-ui text-accent-fg underline underline-offset-2"
        >
          Открыть PDF
        </a>
      </div>
    );
  }
  if (isAudio(item)) {
    return (
      <div className="flex w-full max-w-md flex-col items-center gap-4 rounded-xl border border-border bg-surface p-6">
        <span className="grid size-14 place-items-center rounded-full bg-pressed text-fg-muted">
          <Icon name="audio" size={24} />
        </span>
        <p className="text-center type-heading text-fg [overflow-wrap:anywhere]">{item.name}</p>
        {/* biome-ignore lint/a11y/useMediaCaption: аудиовложение без расшифровки, субтитров нет */}
        <audio controls preload="metadata" src={item.url} className="w-full" />
      </div>
    );
  }
  return (
    <div className="flex w-full max-w-md flex-col items-center gap-4 rounded-xl border border-border bg-surface p-6 text-center">
      <span className="grid size-14 place-items-center rounded-full bg-pressed text-fg-muted">
        <Icon name={attachmentIcon(item.kind)} size={24} />
      </span>
      <div className="flex flex-col gap-1">
        <p className="type-heading text-fg [overflow-wrap:anywhere]">Предпросмотр недоступен</p>
        <p className="type-ui text-fg-muted [overflow-wrap:anywhere]">{item.name}</p>
      </div>
      <a href={item.url} download={item.name} className={cn(buttonVariants({ variant: "primary" }), "gap-2")}>
        <Icon name="download" size={20} />
        Скачать
      </a>
    </div>
  );
}

/**
 * Просмотр вложения на весь экран: изображение (лайтбокс), PDF во встроенном просмотре, аудио-плеер; остальное —
 * «Предпросмотр недоступен» и скачивание. Если вложений несколько, листаются кнопками и стрелками клавиатуры.
 */
export function AttachmentViewer({ items, openId, onOpenChange }: AttachmentViewerProps) {
  const index = openId === null ? -1 : items.findIndex((item) => item.id === openId);
  const item = index >= 0 ? items[index] : undefined;
  const open = item !== undefined;
  const previous = index > 0 ? items[index - 1] : undefined;
  const next = index >= 0 && index < items.length - 1 ? items[index + 1] : undefined;

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.target instanceof HTMLMediaElement || event.target instanceof HTMLInputElement) return;
    if (event.key === "ArrowLeft" && previous) {
      event.preventDefault();
      onOpenChange(previous.id);
    } else if (event.key === "ArrowRight" && next) {
      event.preventDefault();
      onOpenChange(next.id);
    }
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={(value) => !value && onOpenChange(null)}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="motion-overlay fixed inset-0 z-scrim bg-overlay" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          onKeyDown={onKeyDown}
          data-attachment-viewer=""
          className="motion-overlay fixed inset-0 z-layer flex flex-col bg-bg text-fg outline-none"
        >
          {item && (
            <>
              <div className="flex shrink-0 items-center gap-2 border-b border-border px-safe-4 pt-safe-2 pb-2">
                <span className="grid size-10 shrink-0 place-items-center rounded-md bg-pressed text-fg-muted">
                  <Icon name={attachmentIcon(item.kind)} size={20} />
                </span>
                <div className="flex min-w-0 flex-1 flex-col">
                  <DialogPrimitive.Title className="truncate type-subheading text-fg">
                    {item.name}
                  </DialogPrimitive.Title>
                  <span className="truncate type-caption text-fg-muted" data-numeric="">
                    {`${attachmentKindLabel(item.kind)} · ${formatBytes(item.size)}`}
                    {items.length > 1 ? ` · ${index + 1} из ${items.length}` : ""}
                  </span>
                </div>
                {items.length > 1 && (
                  <>
                    <IconButton
                      icon="chevron-left"
                      label="Предыдущее вложение"
                      disabled={!previous}
                      onClick={() => previous && onOpenChange(previous.id)}
                    />
                    <IconButton
                      icon="chevron-right"
                      label="Следующее вложение"
                      disabled={!next}
                      onClick={() => next && onOpenChange(next.id)}
                    />
                  </>
                )}
                <IconLink href={item.url} label="Скачать" icon="download" download={item.name} />
                <IconLink href={item.url} label="Открыть в новой вкладке" icon="external" />
                <DialogPrimitive.Close asChild>
                  <IconButton icon="close" label="Закрыть" />
                </DialogPrimitive.Close>
              </div>
              <div
                key={item.id}
                data-viewer-body=""
                className="flex min-h-0 flex-1 items-center justify-center p-4 pb-safe-4"
              >
                <Body item={item} />
              </div>
            </>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

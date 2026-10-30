import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { DEFAULT_TIME_ZONE, formatTime } from "../../lib/format";
import { attachmentIcon, attachmentKindLabel, type ViewerAttachment } from "../attachments";
import { Badge } from "../badge/badge";
import { type CitationGroup, CitationGroups } from "../citation-groups/citation-groups";
import { formatBytes } from "../file-picker/file-picker";
import { Icon } from "../icon";
import { type LinkResolver, SafeMarkdown } from "../markdown/safe-markdown";

export type MessageStatus = "sending" | "sent" | "queued" | "streaming";

export interface ChatMessageProps {
  role: "user" | "agent";
  /** Текст в markdown (без HTML и ссылок-действий) либо готовое содержимое в `children`. */
  markdown?: string;
  children?: ReactNode;
  /** Время сообщения, мс. */
  at?: number;
  nowMs?: number;
  timeZone?: string;
  /**
   * `sending` — уходит; `queued` — ждёт сети и отправится сама («Отправится, когда появится сеть»);
   * `streaming` — ответ приходит, в конце курсор; `sent` — обычное состояние.
   */
  status?: MessageStatus;
  /** `unsure` — агент не уверен: перед текстом «Не уверен», в тексте сказано, чего не хватает. */
  confidence?: "ok" | "unsure";
  /** Источники ответа, сгруппированные по источнику (одна запись на источник). */
  citationGroups?: readonly CitationGroup[];
  /**
   * Цитата того, на что отвечает сообщение (вопрос агента или задачи): короткая строка над текстом, не больше двух
   * строк. Без неё ответ «Да» или нажатая кнопка не говорят, на что отвечено.
   */
  quote?: string;
  /** Вложения сообщения: изображения — сеткой миниатюр, остальное — строками. Нажатие открывает вьюер. */
  attachments?: readonly ViewerAttachment[];
  onOpenAttachment?: (id: string) => void;
  /** Внутренние ссылки текста (`botai://threads/…`, `botai://attachments/…`): куда вести в приложении. */
  resolveLink?: LinkResolver;
  /** Действия под сообщением (бизнес-кнопки блока). */
  actions?: ReactNode;
  className?: string;
}

function AttachmentList({
  attachments,
  onOpen,
  align,
}: {
  attachments: readonly ViewerAttachment[];
  onOpen?: (id: string) => void;
  align: "start" | "end";
}) {
  const images = attachments.filter((item) => item.kind === "image");
  const rest = attachments.filter((item) => item.kind !== "image");
  return (
    <div
      data-message-attachments=""
      className={cn("flex w-full min-w-0 flex-col gap-2", align === "end" ? "items-end" : "items-start")}
    >
      {images.length > 0 && (
        <ul
          aria-label="Изображения"
          className={cn(
            "grid gap-1.5",
            images.length === 1 ? "w-full max-w-72 grid-cols-1" : "w-full max-w-md grid-cols-2",
            images.length > 2 && "grid-cols-3",
          )}
        >
          {images.map((item) => (
            <li key={item.id} className="min-w-0">
              <button
                type="button"
                aria-label={`Открыть ${item.name}`}
                onClick={() => onOpen?.(item.id)}
                className="block aspect-4/3 w-full cursor-pointer overflow-hidden rounded-md border border-border bg-bg-subtle transition-opacity duration-fast ease-soft hover:opacity-90"
              >
                <img src={item.url} alt={item.name} loading="lazy" className="size-full object-cover" />
              </button>
            </li>
          ))}
        </ul>
      )}
      {rest.length > 0 && (
        <ul aria-label="Файлы" className="flex w-full max-w-sm flex-col gap-1.5">
          {rest.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                aria-label={`Открыть ${item.name}`}
                onClick={() => onOpen?.(item.id)}
                className="flex min-h-12 w-full cursor-pointer items-center gap-3 rounded-md border border-border bg-surface px-3 py-2 text-left transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed"
              >
                <span className="grid size-10 shrink-0 place-items-center rounded-sm bg-pressed text-fg-muted">
                  <Icon name={attachmentIcon(item.kind)} size={20} />
                </span>
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate type-label text-fg">{item.name}</span>
                  <span className="truncate type-caption text-fg-muted" data-numeric="">
                    {`${attachmentKindLabel(item.kind)} · ${formatBytes(item.size)}`}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Сообщение чата, 16 px. Агент — плоский текст на фоне страницы на всю ширину колонки (без карточки и рамки), как в
 * ChatGPT и Claude; если внутри форма вопроса (`children`, `actions`), она остаётся карточкой. Человек — плашка
 * справа. Цитата вопроса и вложения стоят над текстом. Ответ без источника не имитирует источник; неуверенный ответ помечен.
 * Появление — прозрачность и сдвиг 4 px.
 */
export function ChatMessage({
  role,
  markdown,
  children,
  at,
  nowMs = 0,
  timeZone = DEFAULT_TIME_ZONE,
  status = "sent",
  confidence = "ok",
  citationGroups,
  quote,
  attachments,
  onOpenAttachment,
  resolveLink,
  actions,
  className,
}: ChatMessageProps) {
  const isUser = role === "user";
  const hasText = markdown !== undefined && markdown !== "";
  const card = !isUser && Boolean(children || actions);
  const hasBody =
    hasText ||
    Boolean(quote) ||
    Boolean(children) ||
    confidence === "unsure" ||
    Boolean(citationGroups?.length) ||
    Boolean(actions);
  return (
    <article
      data-role={role}
      data-status={status}
      aria-busy={status === "streaming" || undefined}
      className={cn(
        "flex animate-message-in flex-col gap-2",
        isUser ? "items-end" : "items-start",
        className,
      )}
    >
      {attachments && attachments.length > 0 && (
        <AttachmentList
          attachments={attachments}
          onOpen={onOpenAttachment}
          align={isUser ? "end" : "start"}
        />
      )}
      {hasBody && (
        <div
          data-card={card || undefined}
          className={cn(
            "flex min-w-0 flex-col gap-3 [overflow-wrap:anywhere]",
            isUser &&
              "max-w-[min(85%,var(--container-reading))] rounded-lg bg-accent-subtle px-4 py-3 text-fg",
            !isUser && !card && "w-full",
            card && "w-full rounded-lg border border-border bg-surface px-4 py-3",
            status === "queued" && "opacity-80",
          )}
        >
          {confidence === "unsure" && (
            <div>
              <Badge tone="warning" icon="help">
                Не уверен
              </Badge>
            </div>
          )}
          {quote && (
            <blockquote
              data-message-quote=""
              className={cn(
                "m-0 min-w-0 border-l-2 pl-3 type-caption text-fg-muted",
                isUser ? "border-accent-fg" : "border-border-strong",
              )}
            >
              <span className="sr-only">В ответ на: </span>
              <span className="line-clamp-2">{quote}</span>
            </blockquote>
          )}
          {hasText && (
            <SafeMarkdown source={markdown} caret={status === "streaming"} resolveLink={resolveLink} />
          )}
          {markdown !== undefined && !hasText && status === "streaming" && <SafeMarkdown source="" caret />}
          {children}
          {citationGroups && citationGroups.length > 0 && (
            <CitationGroups groups={citationGroups} nowMs={nowMs} timeZone={timeZone} />
          )}
          {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
        </div>
      )}
      <div className="flex items-center gap-2 px-1 type-caption text-fg-subtle">
        {status === "queued" && (
          <span className="inline-flex items-center gap-1">
            <Icon name="offline" size={16} />
            Отправится, когда появится сеть
          </span>
        )}
        {status === "sending" && <span>Отправляется</span>}
        {at !== undefined && status !== "queued" && status !== "sending" && (
          <time dateTime={new Date(at).toISOString()}>{formatTime(at, timeZone)}</time>
        )}
      </div>
    </article>
  );
}

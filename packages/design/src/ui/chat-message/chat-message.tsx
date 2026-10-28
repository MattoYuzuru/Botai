import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { DEFAULT_TIME_ZONE, formatTime } from "../../lib/format";
import { Badge } from "../badge/badge";
import { Icon } from "../icon";
import { SafeMarkdown } from "../markdown/safe-markdown";
import { SourceCitation, type SourceCitationProps } from "../source-citation/source-citation";

export type MessageStatus = "sending" | "sent" | "queued" | "streaming";

export interface ChatCitation extends Omit<SourceCitationProps, "nowMs" | "className"> {
  id: string;
}

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
  citations?: readonly ChatCitation[];
  /** Действия под сообщением (бизнес-кнопки блока). */
  actions?: ReactNode;
  className?: string;
}

/**
 * Сообщение чата, 16 px. Агент — карточка `surface` на всю ширину колонки, человек — плашка справа.
 * Ответ без источника не имитирует источник; неуверенный ответ помечен. Появление — прозрачность и сдвиг 4 px.
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
  citations,
  actions,
  className,
}: ChatMessageProps) {
  const isUser = role === "user";
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
      <div
        className={cn(
          "flex max-w-[min(100%,var(--container-reading))] min-w-0 flex-col gap-3 rounded-lg px-4 py-3 [overflow-wrap:anywhere]",
          isUser ? "bg-accent-subtle text-fg" : "w-full border border-border bg-surface",
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
        {markdown !== undefined && <SafeMarkdown source={markdown} caret={status === "streaming"} />}
        {children}
        {citations && citations.length > 0 && (
          <ul className="flex flex-col gap-2" aria-label="Источники">
            {citations.map(({ id, ...citation }) => (
              <li key={id}>
                <SourceCitation {...citation} nowMs={nowMs} />
              </li>
            ))}
          </ul>
        )}
        {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
      </div>
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

import { cn } from "../../lib/cn";
import { DEFAULT_TIME_ZONE, formatAgo, NBSP } from "../../lib/format";
import { Icon, type IconName } from "../icon";

export interface ContextSegment {
  /** `system`, `profile`, `data`, `documents`, `history`, `free` (agent-harness.md §6). Неизвестный вид рисуется нейтрально. */
  kind: string;
  label: string;
  tokens: number;
}

export interface ContextSourceItem {
  title: string;
  kind: string;
  /** Когда источник получен, мс. `null` — неизвестно. */
  fetchedAt: number | null;
}

export interface ContextMeterProps {
  /** Размер окна контекста, токенов. */
  limit: number;
  /** Сколько занято, токенов. */
  used: number;
  segments: readonly ContextSegment[];
  sources?: readonly ContextSourceItem[];
  /** «Сейчас» для подписи «Получено 5 минут назад». */
  nowMs: number;
  timeZone?: string;
  className?: string;
}

/** Вид сегмента → заливка. Один оттенок акцента и нейтрали: цвет здесь не несёт смысла статуса, различает порядок и легенда. */
const FILL: Record<string, string> = {
  system: "bg-fg-subtle",
  profile: "bg-accent/45",
  data: "bg-accent",
  documents: "bg-accent/70",
  history: "bg-fg-muted",
  free: "bg-bg-subtle",
};
const fillOf = (kind: string) => FILL[kind] ?? "bg-control";

const SOURCE_ICON: Record<string, IconName> = {
  data: "source",
  document: "document",
  documents: "document",
  connection: "connection",
  page: "source",
};

/** «850», «12,4 тыс.», «1 млн»: токены в одной единице, без ложной точности. */
export function formatTokens(value: number): string {
  const n = Math.max(0, Math.round(value));
  const trim = (x: number) => String(Math.round(x * 10) / 10).replace(".", ",");
  if (n >= 1_000_000) return `${trim(n / 1_000_000)}${NBSP}млн`;
  if (n >= 1_000) return `${trim(n / 1_000)}${NBSP}тыс.`;
  return String(n);
}

export interface ContextMeterBarProps {
  limit: number;
  segments: readonly ContextSegment[];
  className?: string;
}

/**
 * Одна плоская полоса, разделённая по сегментам, как `/context` в Claude Code. Ширина сегмента — его доля окна
 * контекста; остаток — пустая часть трека. Разделители в 1 px отделяют сегменты друг от друга.
 */
export function ContextMeterBar({ limit, segments, className }: ContextMeterBarProps) {
  const total = Math.max(limit, 1);
  const shown = segments.filter((segment) => segment.kind !== "free" && segment.tokens > 0);
  return (
    <div
      data-context-bar=""
      className={cn("flex h-2 w-full gap-px overflow-hidden rounded-full bg-bg-subtle", className)}
    >
      {shown.map((segment) => (
        <span
          key={`${segment.kind}-${segment.label}`}
          data-segment={segment.kind}
          className={cn("h-full min-w-0.5 first:rounded-l-full last:rounded-r-full", fillOf(segment.kind))}
          style={{ width: `${Math.min(100, (segment.tokens / total) * 100)}%` }}
        />
      ))}
    </div>
  );
}

/**
 * Что агент держит в контексте этого чата: полоса по сегментам, легенда с числом токенов и список подключённых
 * в ответ источников. Все числа — с сервера (`GET /chat/threads/{id}/context`), своих оценок здесь нет.
 */
export function ContextMeter({
  limit,
  used,
  segments,
  sources = [],
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  className,
}: ContextMeterProps) {
  const legend = segments.filter((segment) => segment.kind !== "free" && segment.tokens > 0);
  return (
    <div className={cn("flex flex-col gap-5", className)} data-context-meter="">
      <div className="flex flex-col gap-3">
        <p className="type-ui text-fg" data-numeric="">
          {formatTokens(used)} из {formatTokens(limit)} токенов
        </p>
        <ContextMeterBar limit={limit} segments={segments} />
        {legend.length > 0 ? (
          <ul className="flex flex-col gap-1.5" aria-label="Из чего состоит контекст">
            {legend.map((segment) => (
              <li key={`${segment.kind}-${segment.label}`} className="flex items-center gap-2 type-ui">
                <span aria-hidden="true" className={cn("size-3 shrink-0 rounded-xs", fillOf(segment.kind))} />
                <span className="min-w-0 flex-1 text-fg">{segment.label}</span>
                <span className="text-fg-muted" data-numeric="">
                  {formatTokens(segment.tokens)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="type-caption text-fg-muted">Пока в контексте только правила и профиль.</p>
        )}
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="type-subheading text-fg">Источники ответа</h3>
        {sources.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {sources.map((source) => (
              <li
                key={`${source.kind}-${source.title}`}
                className="flex items-start gap-2 rounded-md bg-bg-subtle px-3 py-2"
              >
                <Icon name={SOURCE_ICON[source.kind] ?? "source"} size={16} className="mt-0.5 text-info" />
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="type-label text-fg">{source.title}</span>
                  {source.fetchedAt !== null && (
                    <time
                      className="type-caption text-fg-muted"
                      dateTime={new Date(source.fetchedAt).toISOString()}
                    >
                      {`Получено ${formatAgo(source.fetchedAt, nowMs, timeZone)}`}
                    </time>
                  )}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="type-caption text-fg-muted">В этом чате агент пока ничего не подключал к ответам.</p>
        )}
      </div>
    </div>
  );
}

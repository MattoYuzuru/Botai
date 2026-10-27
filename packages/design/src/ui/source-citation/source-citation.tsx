import type { MouseEvent } from "react";
import { cn } from "../../lib/cn";
import { DEFAULT_TIME_ZONE, formatAgo } from "../../lib/format";
import { displayHost, safeHttpUrl } from "../../lib/safe-url";
import { Badge } from "../badge/badge";
import { Icon, type IconName } from "../icon";

export interface SourceCitationProps {
  title: string;
  url: string | null | undefined;
  /** Когда страница была получена, мс. */
  fetchedAt: number;
  nowMs: number;
  timeZone?: string;
  /** Идентификатор пространства (атрибут), название вуза — отдельным пропом. */
  spaceId?: string;
  spaceTitle?: string;
  /** Фрагмент, на который опирается ответ. */
  snippet?: string;
  /** Снимок помечен устаревшим на сервере. */
  stale?: boolean;
  /**
   * Внутренняя ссылка на документ или подключение пользователя (`botai://documents/<id>`): открывается в
   * приложении, в той же вкладке. Вместо хоста показывается `label` («Открыть в «Учёбе»»).
   */
  internal?: { href: string; label: string; icon?: IconName; onOpen?: () => void };
  className?: string;
}

/**
 * Источник ответа: название, вуз и время выборки. Недоступный адрес не кликабелен («Источник недоступен»),
 * но подпись и время остаются: ответ без источника не имитирует источник, а пропавший источник не прячется.
 */
export function SourceCitation({
  title,
  url,
  fetchedAt,
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  spaceId,
  spaceTitle,
  snippet,
  stale = false,
  internal,
  className,
}: SourceCitationProps) {
  const safe = internal ? null : safeHttpUrl(url);
  const body = (
    <>
      <Icon name={internal?.icon ?? "source"} size={16} className="mt-0.5 text-info" />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="truncate type-label text-fg">{title}</span>
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">
          {spaceTitle && <Badge tone="neutral">{spaceTitle}</Badge>}
          {internal ? (
            <span className="truncate">{internal.label}</span>
          ) : safe ? (
            <span className="truncate">{displayHost(safe)}</span>
          ) : (
            <span>Источник недоступен</span>
          )}
          <time
            dateTime={new Date(fetchedAt).toISOString()}
          >{`Получено ${formatAgo(fetchedAt, nowMs, timeZone)}`}</time>
          {stale && <Badge tone="warning">Устарело</Badge>}
        </span>
        {snippet && <span className="line-clamp-2 type-caption text-fg-subtle">{snippet}</span>}
      </span>
      {safe && <Icon name="external" size={16} className="mt-0.5 text-fg-subtle" />}
      {internal && <Icon name="chevron-right" size={16} className="mt-0.5 text-fg-subtle" />}
    </>
  );
  const shared = "flex min-h-11 items-start gap-2 rounded-md bg-bg-subtle px-3 py-2";
  const interactive = "transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed";
  if (internal) {
    // Обычный щелчок открывает экран внутри приложения; щелчок с Ctrl/Cmd и средняя кнопка — как у ссылки.
    const open = (event: MouseEvent<HTMLAnchorElement>) => {
      if (!internal.onOpen || event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      internal.onOpen();
    };
    return (
      <a
        href={internal.href}
        onClick={open}
        data-space-id={spaceId}
        data-internal=""
        className={cn(shared, interactive, className)}
      >
        {body}
      </a>
    );
  }
  if (!safe) {
    return (
      <div data-space-id={spaceId} data-unavailable="" className={cn(shared, className)}>
        {body}
      </div>
    );
  }
  return (
    <a
      href={safe.href}
      target="_blank"
      rel="noopener noreferrer"
      data-space-id={spaceId}
      className={cn(shared, interactive, className)}
    >
      {body}
    </a>
  );
}

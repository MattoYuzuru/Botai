import { type MouseEvent, type ReactNode, useId, useState } from "react";
import { cn } from "../../lib/cn";
import { countRu, DEFAULT_TIME_ZONE, formatAgo } from "../../lib/format";
import { safeHttpUrl } from "../../lib/safe-url";
import { Badge } from "../badge/badge";
import { Icon, type IconName } from "../icon";

export interface CitationItem {
  id: string;
  /** Название страницы, записи или документа. */
  title: string;
  url: string | null;
  /** Внутренняя ссылка приложения (`botai://…`): открывается в той же вкладке. */
  internal?: { href: string; label: string; icon?: IconName; onOpen?: () => void };
  snippet?: string;
}

export interface CitationGroup {
  id: string;
  /** Название источника («Личный кабинет ВШЭ»), не адрес сайта. */
  label: string;
  spaceTitle?: string;
  /** Когда данные получены, мс. */
  fetchedAt: number;
  stale?: boolean;
  items: readonly CitationItem[];
}

export interface CitationGroupsProps {
  groups: readonly CitationGroup[];
  nowMs: number;
  timeZone?: string;
  className?: string;
}

const row = "flex min-h-11 w-full items-start gap-2 rounded-md bg-bg-subtle px-3 py-2 text-left";
const interactive =
  "cursor-pointer transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed";

/** Обычный щелчок открывает экран приложения; Ctrl/Cmd, Shift и средняя кнопка остаются поведением ссылки. */
function openInternal(event: MouseEvent<HTMLAnchorElement>, onOpen: (() => void) | undefined) {
  if (!onOpen || event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  onOpen();
}

function ItemLink({
  item,
  className,
  children,
}: {
  item: CitationItem;
  className?: string;
  children: ReactNode;
}) {
  if (item.internal) {
    return (
      <a
        href={item.internal.href}
        data-internal=""
        onClick={(event) => openInternal(event, item.internal?.onOpen)}
        className={className}
      >
        {children}
      </a>
    );
  }
  const safe = safeHttpUrl(item.url);
  if (!safe) return <div className={className}>{children}</div>;
  return (
    <a href={safe.href} target="_blank" rel="noopener noreferrer" className={className}>
      {children}
    </a>
  );
}

const available = (item: CitationItem) => Boolean(item.internal) || safeHttpUrl(item.url) !== null;

function Meta({ group, nowMs, timeZone }: { group: CitationGroup; nowMs: number; timeZone: string }) {
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">
      {group.spaceTitle && <Badge tone="neutral">{group.spaceTitle}</Badge>}
      {group.items.length > 1 && (
        <span data-numeric="">{countRu(group.items.length, ["страница", "страницы", "страниц"])}</span>
      )}
      <time dateTime={new Date(group.fetchedAt).toISOString()}>
        {`Получено ${formatAgo(group.fetchedAt, nowMs, timeZone)}`}
      </time>
      {group.stale && <Badge tone="warning">Устарело</Badge>}
    </span>
  );
}

function SingleGroup({ group, nowMs, timeZone }: { group: CitationGroup; nowMs: number; timeZone: string }) {
  const item = group.items[0];
  if (!item) return null;
  const open = available(item);
  const icon = item.internal?.icon ?? "source";
  return (
    <ItemLink item={item} className={cn(row, open && interactive)}>
      <Icon name={icon} size={16} className="mt-0.5 text-info" />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="truncate type-label text-fg">{group.label}</span>
        {item.title !== group.label && (
          <span className="truncate type-caption text-fg-muted">{item.title}</span>
        )}
        <Meta group={group} nowMs={nowMs} timeZone={timeZone} />
        {item.internal ? (
          <span className="truncate type-caption text-fg-muted">{item.internal.label}</span>
        ) : (
          !open && <span className="type-caption text-fg-muted">Источник недоступен</span>
        )}
        {item.snippet && <span className="line-clamp-2 type-caption text-fg-subtle">{item.snippet}</span>}
      </span>
      {open && !item.internal && <Icon name="external" size={16} className="mt-0.5 text-fg-subtle" />}
      {item.internal && <Icon name="chevron-right" size={16} className="mt-0.5 text-fg-subtle" />}
    </ItemLink>
  );
}

function MultiGroup({ group, nowMs, timeZone }: { group: CitationGroup; nowMs: number; timeZone: string }) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  return (
    <>
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={listId}
        onClick={() => setExpanded((value) => !value)}
        className={cn(row, interactive)}
      >
        <Icon name="source" size={16} className="mt-0.5 text-info" />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate type-label text-fg">{group.label}</span>
          <Meta group={group} nowMs={nowMs} timeZone={timeZone} />
        </span>
        <Icon name={expanded ? "chevron-up" : "chevron-down"} size={16} className="mt-0.5 text-fg-subtle" />
      </button>
      {expanded && (
        <ul id={listId} aria-label={`Страницы: ${group.label}`} className="mt-1 flex flex-col gap-1 pl-3">
          {group.items.map((item) => {
            const open = available(item);
            return (
              <li key={item.id}>
                <ItemLink item={item} className={cn(row, "bg-transparent", open && interactive)}>
                  <Icon name={item.internal?.icon ?? "source"} size={16} className="mt-0.5 text-info" />
                  <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className="type-ui text-fg [overflow-wrap:anywhere]">{item.title}</span>
                    {item.internal && (
                      <span className="type-caption text-fg-muted">{item.internal.label}</span>
                    )}
                    {!open && <span className="type-caption text-fg-muted">Источник недоступен</span>}
                    {item.snippet && (
                      <span className="line-clamp-2 type-caption text-fg-subtle">{item.snippet}</span>
                    )}
                  </span>
                  {open && !item.internal && (
                    <Icon name="external" size={16} className="mt-0.5 text-fg-subtle" />
                  )}
                  {item.internal && <Icon name="chevron-right" size={16} className="mt-0.5 text-fg-subtle" />}
                </ItemLink>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

/**
 * Источники ответа, сгруппированные по источнику: одна запись на источник с его названием (не хостом),
 * временем получения и числом страниц. Одна страница — вся запись ссылка; несколько — запись раскрывается списком.
 * Недоступный адрес не кликабелен («Источник недоступен»), подпись и время остаются.
 */
export function CitationGroups({
  groups,
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  className,
}: CitationGroupsProps) {
  if (groups.length === 0) return null;
  return (
    <ul aria-label="Источники" data-citation-groups="" className={cn("flex flex-col gap-2", className)}>
      {groups.map((group) => (
        <li key={group.id} data-citation-group={group.id}>
          {group.items.length > 1 ? (
            <MultiGroup group={group} nowMs={nowMs} timeZone={timeZone} />
          ) : (
            <SingleGroup group={group} nowMs={nowMs} timeZone={timeZone} />
          )}
        </li>
      ))}
    </ul>
  );
}

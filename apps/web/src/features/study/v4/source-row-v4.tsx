import { Badge, Button, formatUpdated, ListRow } from "@botai/design";
import { Link } from "@tanstack/react-router";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { sourceChatLinks } from "./chat-links-v4";
import { describeSourceKind, describeSourceStatus, kindsLabel, sourceIcon } from "./status";
import type { ScheduleRole } from "./university-panel-v4";
import type { StudySource } from "./use-study-sources";

/**
 * Строка источника вуза (03-sources-and-ux.md §5.2): иконка природы, название, чипы видов, статус словами
 * и иконкой, «обновлено N назад», роль в расписании и быстрые действия для того, что требует человека.
 * Вся строка открывает карточку источника; отдельного «Источников»-раздела больше нет.
 */
export function SourceRow({
  source,
  space,
  nowMs,
  scheduleRole,
  onOpen,
}: {
  source: StudySource;
  space: SpaceInfo;
  nowMs: number;
  scheduleRole: ScheduleRole | null;
  onOpen: (source: StudySource) => void;
}) {
  const status = describeSourceStatus(source.status);
  const lastSync = source.lastSyncAt ? Date.parse(source.lastSyncAt) : Number.NaN;
  const updated = Number.isFinite(lastSync)
    ? formatUpdated(lastSync, nowMs, space.timeZone)
    : "Ещё не обновлялся";
  const kinds = kindsLabel(source.kinds);
  const chat = sourceChatLinks(source, space.title);
  const quickAction = status.needsUser || source.status === "error" ? chat.repair : null;
  const quickLabel =
    source.status === "needs_reauth"
      ? "Войти снова"
      : source.status === "needs_link"
        ? "Новая ссылка"
        : "Починить";

  return (
    <li
      className="flex min-w-0 items-center gap-1"
      data-source-id={source.id}
      data-source-status={source.status}
      data-source-mode={source.mode}
    >
      <ListRow
        className="min-w-0 flex-1"
        icon={sourceIcon(source.kinds)}
        title={source.title}
        onOpen={() => onOpen(source)}
        meta={
          <>
            <Badge tone={status.tone} icon={status.icon} data-source-status-label="">
              {status.label}
            </Badge>
            <span>{kinds || describeSourceKind(source.kinds[0] ?? "other").label}</span>
            <time dateTime={source.lastSyncAt ?? undefined}>{updated}</time>
            {scheduleRole && (
              <Badge tone={scheduleRole === "primary" ? "accent" : "neutral"}>
                {scheduleRole === "primary" ? "Основное расписание" : "Дополнительное расписание"}
              </Badge>
            )}
            {source.errorDetail && (
              <span className="min-w-0 truncate text-warning-fg" data-source-error="">
                {source.errorDetail}
              </span>
            )}
          </>
        }
        aria-label={`${source.title}: ${status.label}. ${status.detail}${
          scheduleRole === "primary"
            ? " Основное расписание."
            : scheduleRole === "secondary"
              ? " Дополнительное расписание."
              : ""
        }`}
      />
      {quickAction && (
        <Button variant="ghost" size="sm" asChild className="shrink-0" data-touch-expanded="">
          <Link {...quickAction} aria-label={`${quickLabel}: ${source.title}`}>
            {quickLabel}
          </Link>
        </Button>
      )}
    </li>
  );
}

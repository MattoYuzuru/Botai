import {
  Badge,
  Banner,
  Button,
  Card,
  formatAgo,
  Icon,
  ListRow,
  ListRowBody,
  listRowClass,
  Skeleton,
} from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { sectionsQuery } from "@/api/queries";
import { priorityQuery } from "@/api/sources";
import { documentsQuery } from "@/features/data/queries";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { visibleSections } from "../registry";
import { addSourceLinkV4, askUniversityLink, sourceChatLinks } from "./chat-links-v4";
import { SourceRow } from "./source-row-v4";
import { dataKindsOf, describeSourceStatus, summarizeSources } from "./status";
import { type StudySource, useStudySources } from "./use-study-sources";

const DOCUMENT_KIND_LABEL: Record<string, string> = {
  file: "Файл",
  sheet: "Таблица",
  page: "Страница",
};
const SHOWN_DOCUMENTS = 3;

/**
 * Блок вуза на «Учёбе» (03-sources-and-ux.md §5.2): шапка со сводным статусом, источники строками,
 * «Добавить источник» и «Спросить агента» — оба ведут в тот же агентский тред, документы вуза (это
 * артефакты его источников, отдельного места для них нет) и производные разделы с данными.
 */
export function UniversityPanelV4({
  space,
  nowMs,
  onOpenSource,
}: {
  space: SpaceInfo;
  nowMs: number;
  onOpenSource: (source: StudySource) => void;
}) {
  const sections = useQuery({ ...sectionsQuery(space.id), refetchInterval: 60_000 });
  const visible = sections.data ? visibleSections(sections.data) : null;
  const { sources, mode, loading, error } = useStudySources(space);
  const documents = useQuery(documentsQuery(space.id));
  const abbreviated = space.shortTitle !== space.title;
  const summary = summarizeSources(sources.map((source) => source.status));
  const attention = sources.find((source) => describeNeedsAction(source));
  const attentionChat = attention ? sourceChatLinks(attention, space.title).repair : null;
  const lastSync = latestSync(sources);
  const docs = (documents.data?.data ?? []).slice().sort((a, b) => b.fetchedAt.localeCompare(a.fetchedAt));
  const scheduleRole = useScheduleRole(space, sources);

  return (
    <section
      aria-labelledby={`space-v4-${space.id}`}
      data-space-id={space.id}
      data-source-mode={mode}
      className="flex flex-col gap-2"
    >
      <div className="flex flex-col gap-1 px-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h2 id={`space-v4-${space.id}`} className="type-heading text-fg">
            {space.title}
          </h2>
          {abbreviated && <Badge tone="neutral">{space.shortTitle}</Badge>}
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">
          {summary ? (
            <Badge tone={summary.tone} icon={summary.icon}>
              {summary.label}
            </Badge>
          ) : loading || error ? (
            // Пока неизвестно, что с источниками, «пусто» не утверждаем: это либо загрузка, либо сбой.
            <span>{error ? "Источники не загрузились" : "Загружаем источники"}</span>
          ) : (
            <span>Источников пока нет</span>
          )}
          {lastSync !== null && (
            <span>Последнее обновление: {formatAgo(lastSync, nowMs, space.timeZone)}</span>
          )}
          {attention && attentionChat && (
            <Button variant="ghost" size="sm" asChild data-touch-expanded="">
              <Link {...attentionChat} aria-label={actionLabel(attention)}>
                {actionLabel(attention)}
              </Link>
            </Button>
          )}
        </div>
      </div>

      <Card padding="sm" data-part="sources-v4" className="flex flex-col gap-2">
        {error ? (
          <Banner tone="info" icon="sync" live>
            Не удалось загрузить источники. Обновление идёт в фоне — данные появятся сами.
          </Banner>
        ) : loading ? (
          <div role="status" aria-busy="true" className="flex flex-col gap-2 p-2">
            <span className="sr-only">Загрузка источников</span>
            <Skeleton className="h-14 rounded-md" />
            <Skeleton className="h-14 rounded-md" />
          </div>
        ) : sources.length === 0 ? (
          <p className="px-3 py-3 type-ui text-fg-muted">
            У вуза пока нет источников: добавьте сайт, кабинет, календарь, файл или почту — агент сам
            разберётся.
          </p>
        ) : (
          <ul aria-label={`Источники: ${space.shortTitle}`}>
            {sources.map((source) => (
              <SourceRow
                key={source.id}
                source={source}
                space={space}
                nowMs={nowMs}
                scheduleRole={scheduleRole.of(source)}
                onOpen={onOpenSource}
              />
            ))}
          </ul>
        )}
        <div className="flex flex-wrap gap-x-2">
          <Button variant="secondary" asChild>
            <Link {...addSourceLinkV4(space.title)}>
              <Icon name="plus" size={20} />
              Добавить источник
            </Link>
          </Button>
          <Button variant="ghost" leadingIcon="chat" asChild>
            <Link {...askUniversityLink(space.title)}>Спросить агента</Link>
          </Button>
        </div>
      </Card>

      {docs.length > 0 && (
        <Card padding="sm" data-part="documents-v4" className="flex flex-col">
          <div className="flex items-center justify-between gap-2 px-3 pt-2 pb-1">
            <h3 className="type-subheading text-fg">Документы</h3>
            <span className="type-caption text-fg-muted">{docs.length}</span>
          </div>
          <ul aria-label={`Документы: ${space.shortTitle}`}>
            {docs.slice(0, SHOWN_DOCUMENTS).map((document) => (
              <li key={document.id}>
                <ListRow
                  icon="document"
                  title={document.title}
                  meta={
                    <>
                      <span>{DOCUMENT_KIND_LABEL[document.kind] ?? "Документ"}</span>
                      <time dateTime={document.fetchedAt}>
                        {formatAgo(Date.parse(document.fetchedAt), nowMs, space.timeZone)}
                      </time>
                    </>
                  }
                />
              </li>
            ))}
          </ul>
          <p className="px-3 pb-2 type-caption text-fg-subtle">
            Документы приходят из источников вуза — отдельного места у них нет. Добавить файл или ссылку можно
            через «Добавить источник».
          </p>
        </Card>
      )}

      <Card padding="sm" data-part="sections-v4">
        {visible === null ? (
          <div role="status" aria-busy="true" className="flex flex-col gap-2 p-2">
            <span className="sr-only">Загрузка разделов</span>
            <Skeleton className="h-14 rounded-md" />
            <Skeleton className="h-14 rounded-md" />
          </div>
        ) : visible.length === 0 ? (
          <p className="px-3 py-3 type-ui text-fg-muted">
            Данных пока нет: они появятся, когда источники их отдадут.
          </p>
        ) : (
          <ul aria-label={`Разделы: ${space.shortTitle}`}>
            {visible.map(({ def, count }) => (
              <li key={def.kind}>
                <Link
                  to="/study/$kind"
                  params={{ kind: def.kind }}
                  search={{ space: space.id }}
                  className={listRowClass}
                  aria-label={`${def.title}: ${count}`}
                >
                  <ListRowBody icon={def.icon} title={def.title} trailing={count} chevron />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </section>
  );
}

export type ScheduleRole = "primary" | "secondary";

interface ScheduleRoleView {
  /** Роль источника в склейке расписания вуза; `null` — единственный источник или выбор ещё не сделан. */
  of: (source: StudySource) => ScheduleRole | null;
}

/**
 * Роль источника в расписании (FR-054, styleguide §9): подписывается, только когда у вуза несколько
 * живых источников с занятиями и владелец уже выбрал основной. Выбор не выдумывается: без сохранённого
 * порядка порядок по умолчанию — правило сервера, а не интерфейса.
 */
function useScheduleRole(space: SpaceInfo, sources: readonly StudySource[]): ScheduleRoleView {
  const schedule = sources.filter((source) => dataKindsOf(source.kinds).includes("schedule"));
  const live = schedule.filter((source) => !describeSourceStatus(source.status).disabled);
  const priority = useQuery({
    ...priorityQuery(space.id, "schedule"),
    enabled: live.length > 1,
  });
  const order = priority.data?.sourceIds ?? [];
  const primary = live.find((source) => source.id === order[0]) ?? null;
  return {
    of: (source) => {
      if (!primary || !live.some((row) => row.id === source.id)) return null;
      return source.id === primary.id ? "primary" : "secondary";
    },
  };
}

function describeNeedsAction(source: StudySource): boolean {
  return (
    source.status === "needs_reauth" ||
    source.status === "needs_link" ||
    source.status === "waiting_user" ||
    source.status === "error"
  );
}

function actionLabel(source: StudySource): string {
  if (source.status === "needs_reauth") return "Войти снова";
  if (source.status === "needs_link") return "Новая ссылка";
  return "Починить";
}

function latestSync(sources: readonly StudySource[]): number | null {
  const times = sources
    .map((source) => (source.lastSyncAt ? Date.parse(source.lastSyncAt) : Number.NaN))
    .filter((time) => Number.isFinite(time));
  return times.length > 0 ? Math.max(...times) : null;
}

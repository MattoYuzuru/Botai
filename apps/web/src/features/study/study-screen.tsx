import {
  Badge,
  Button,
  Card,
  Icon,
  ListRowBody,
  listRowClass,
  Skeleton,
  SourceStatus,
  useNow,
  useOnline,
} from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { sectionsQuery } from "@/api/queries";
import { Screen } from "@/app/screen";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { CONNECT_DRAFT } from "@/features/chat/drafts";
import { describeConnection } from "@/features/data/connection-status";
import { resolveDataState } from "@/features/data/data-state";
import { ScreenSyncIndicator, SourceNotices } from "@/features/data/source-notices";
import { type SpaceInfo, useScope, useSpaceDirectory } from "@/features/data/use-space-directory";
import { AccountSettings } from "@/features/settings/account";
import { ConnectPrompt } from "@/features/today/connect-prompt";
import { type StudyKind, visibleSections } from "./registry";

/**
 * «Учёба» (`/study`): пространства вузов, у каждого — динамические разделы (FR-014) в фиксированном порядке:
 * Предметы, Оценки, Преподаватели, Аудитории, Новости, Документы, Подключения. Раздел виден, только если у
 * подключений есть данные этого вида; «Подключения» — всегда. Профиль и настройки — внизу экрана (§7.1).
 */
export function StudyScreen() {
  const directory = useSpaceDirectory();
  const scope = useScope(directory);
  const nowMs = useNow(30_000);
  const online = useOnline();
  const state = resolveDataState({
    hasData: directory.connectionsLoaded,
    isEmpty: false,
    online,
    snapshot: false,
    failed: false,
    sources: scope.spaces.map((space) => space.health.state).filter((value) => value !== "none"),
    staleItems: false,
  });
  const trailing = (
    <>
      <ScreenSyncIndicator />
      <ConnectedSpaceSwitcher />
    </>
  );

  return (
    <Screen title="Учёба" trailing={trailing}>
      {!directory.loaded ? (
        <div
          data-screen="study"
          data-state="loading"
          role="status"
          aria-busy="true"
          className="flex flex-col gap-3"
        >
          <span className="sr-only">Загрузка</span>
          <Skeleton width="40%" className="h-6" />
          <Skeleton className="h-14 rounded-lg" />
          <Skeleton className="h-14 rounded-lg" />
        </div>
      ) : directory.none ? (
        <ConnectPrompt />
      ) : (
        <div data-screen="study" data-state={state} className="flex flex-col gap-6">
          <SourceNotices spaces={scope.spaces} nowMs={nowMs} />
          {scope.spaces.map((space) => (
            <SpacePanel key={space.id} space={space} nowMs={nowMs} showTitle />
          ))}
          <Button variant="ghost" asChild className="self-start">
            <Link to="/chat" search={{ draft: CONNECT_DRAFT }}>
              <Icon name="plus" size={20} />
              Подключить ещё вуз
            </Link>
          </Button>
        </div>
      )}
      <AccountSettings />
    </Screen>
  );
}

/** Вуз и его разделы: заголовок с состоянием источников и список разделов, у которых есть данные. */
function SpacePanel({ space, nowMs, showTitle }: { space: SpaceInfo; nowMs: number; showTitle: boolean }) {
  const sections = useQuery({ ...sectionsQuery(space.id), refetchInterval: 60_000 });
  const visible = sections.data ? visibleSections(sections.data, space.connections.length) : null;
  const attention = space.connections.filter(
    (connection) => describeConnection(connection, nowMs, { shortTitle: space.shortTitle }).attention,
  ).length;

  return (
    <section aria-labelledby={`space-${space.id}`} data-space-id={space.id} className="flex flex-col gap-2">
      <div className="flex flex-col gap-1 px-1">
        <h2 id={`space-${space.id}`} className={showTitle ? "type-heading text-fg" : "sr-only"}>
          {space.title}
        </h2>
        {space.health.state !== "none" && (
          <SourceStatus
            state={space.health.state}
            spaceId={space.id}
            spaceTitle={space.shortTitle}
            updatedAt={space.health.updatedAt}
            nowMs={nowMs}
            timeZone={space.timeZone}
          />
        )}
      </div>
      <Card padding="sm">
        {visible === null ? (
          <div role="status" aria-busy="true" className="flex flex-col gap-2">
            <span className="sr-only">Загрузка разделов</span>
            <Skeleton className="h-14 rounded-md" />
            <Skeleton className="h-14 rounded-md" />
            <Skeleton className="h-14 rounded-md" />
          </div>
        ) : (
          <ul aria-label={`Разделы: ${space.shortTitle}`}>
            {visible.map(({ def, count }) => (
              <li key={def.kind}>
                <Link
                  to="/study/$kind"
                  params={{ kind: def.kind satisfies StudyKind }}
                  search={{ space: space.id }}
                  className={listRowClass}
                >
                  <ListRowBody
                    icon={def.icon}
                    title={def.title}
                    meta={
                      def.kind === "connections" && attention > 0 ? (
                        <Badge tone="warning" icon="warning">
                          Нужно внимание: {attention}
                        </Badge>
                      ) : undefined
                    }
                    trailing={count}
                    chevron
                  />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </section>
  );
}

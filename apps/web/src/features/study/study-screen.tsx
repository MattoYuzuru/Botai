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
import { useState } from "react";
import { sectionsQuery } from "@/api/queries";
import type { Connection } from "@/api/types";
import { Screen } from "@/app/screen";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { addSourceLink, connectUniversityLink } from "@/features/data/chat-links";
import { resolveDataState } from "@/features/data/data-state";
import { ScreenSyncIndicator, SourceNotices } from "@/features/data/source-notices";
import { type SpaceInfo, useScope, useSpaceDirectory } from "@/features/data/use-space-directory";
import { AccountSettings } from "@/features/settings/account";
import { ConnectPrompt } from "@/features/today/connect-prompt";
import { AddDocumentSheet } from "./add-document-sheet";
import { ConnectionSheet } from "./connection-sheet";
import { type StudyKind, visibleSections } from "./registry";
import { SourceList } from "./source-list";

/**
 * «Учёба» (`/study`): вуз — единица со многими источниками. У каждого вуза: название, сокращение и сводный
 * статус; список его источников (личный кабинет, учебная платформа, документация, таблицы, документы) со
 * статусом и временем обновления; «Добавить источник» открывает новый чат с готовой просьбой, «Добавить
 * документ» — шит (есть и у вуза без документов); ниже разделы
 * с данными (Предметы, Оценки, … «Документы и вложения») в фиксированном порядке (FR-014). Профиль и
 * настройки — внизу экрана (§7.1).
 */
export function StudyScreen() {
  const directory = useSpaceDirectory();
  const scope = useScope(directory);
  const nowMs = useNow(30_000);
  const online = useOnline();
  const [openId, setOpenId] = useState<string | null>(null);
  // Вуз, в который добавляют документ: кнопка есть в панели каждого вуза, даже если документов ещё нет.
  // `key` шита — вуз: у другой панели форма открывается заново, а при закрытии анимация не обрывается.
  const [addingTo, setAddingTo] = useState<{ spaceId: string; open: boolean } | null>(null);
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
  const opened = scope.spaces
    .flatMap((space) => space.connections.map((connection) => ({ connection, space })))
    .find((row) => row.connection.id === openId);

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
            <UniversityPanel
              key={space.id}
              space={space}
              nowMs={nowMs}
              onOpenSource={(connection: Connection) => setOpenId(connection.id)}
              onAddDocument={() => setAddingTo({ spaceId: space.id, open: true })}
            />
          ))}
          <Button variant="ghost" asChild className="self-start">
            <Link {...connectUniversityLink()}>
              <Icon name="plus" size={20} />
              Подключить ещё вуз
            </Link>
          </Button>
        </div>
      )}
      <ConnectionSheet
        connection={opened?.connection ?? null}
        space={opened?.space}
        nowMs={nowMs}
        onClose={() => setOpenId(null)}
      />
      <AddDocumentSheet
        key={addingTo?.spaceId ?? "none"}
        open={addingTo?.open ?? false}
        onOpenChange={(open) => setAddingTo((current) => current && { ...current, open })}
        spaces={scope.spaces}
        defaultSpaceId={addingTo?.spaceId}
      />
      <AccountSettings />
    </Screen>
  );
}

/** Вуз: название и статус, его источники и разделы с данными. */
function UniversityPanel({
  space,
  nowMs,
  onOpenSource,
  onAddDocument,
}: {
  space: SpaceInfo;
  nowMs: number;
  onOpenSource: (connection: Connection) => void;
  onAddDocument: () => void;
}) {
  const sections = useQuery({ ...sectionsQuery(space.id), refetchInterval: 60_000 });
  const visible = sections.data ? visibleSections(sections.data) : null;
  const abbreviated = space.shortTitle !== space.title;

  return (
    <section aria-labelledby={`space-${space.id}`} data-space-id={space.id} className="flex flex-col gap-2">
      <div className="flex flex-col gap-1 px-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h2 id={`space-${space.id}`} className="type-heading text-fg">
            {space.title}
          </h2>
          {abbreviated && <Badge tone="neutral">{space.shortTitle}</Badge>}
        </div>
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

      <Card padding="sm" data-part="sources" className="flex flex-col">
        {space.connections.length === 0 ? (
          <p className="px-3 py-3 type-ui text-fg-muted">
            Источников пока нет: добавьте личный кабинет, учебную платформу, таблицу или документ.
          </p>
        ) : (
          <SourceList space={space} nowMs={nowMs} onOpen={onOpenSource} />
        )}
        <div className="flex flex-wrap gap-x-2">
          <Button variant="ghost" asChild>
            <Link {...addSourceLink(space.title)}>
              <Icon name="plus" size={20} />
              Добавить источник
            </Link>
          </Button>
          <Button variant="ghost" leadingIcon="document" onClick={onAddDocument}>
            Добавить документ
          </Button>
        </div>
      </Card>

      <Card padding="sm" data-part="sections">
        {visible === null ? (
          <div role="status" aria-busy="true" className="flex flex-col gap-2">
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
                  params={{ kind: def.kind satisfies StudyKind }}
                  search={{ space: space.id }}
                  className={listRowClass}
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

import { Badge, Button, Card, EmptyState, Icon, ListRow, useNow } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { connectionKindTitle, describeConnection, originLabel } from "@/features/data/connection-status";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { AddDocumentSheet } from "../add-document-sheet";
import { ConnectionSheet } from "../connection-sheet";
import { type SectionContext, SectionFrame } from "../section-frame";
import { useSectionState } from "../use-section";

/**
 * «Подключения»: источники каждого вуза со статусом человеческим языком. Нажатие открывает управление:
 * ссылка календаря или ключ, «Забыть пароль», удаление. Здесь же — «Добавить документ». Обновление и повторы
 * идут сами, кнопки «Обновить» нет.
 */
export function ConnectionsSection({ ctx }: { ctx: SectionContext }) {
  const nowMs = useNow(30_000);
  const spaces = ctx.scope.spaces;
  const total = spaces.reduce((sum, space) => sum + space.connections.length, 0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const state = useSectionState({
    hasData: ctx.directory.connectionsLoaded,
    isEmpty: total === 0,
    snapshot: false,
    failed: false,
    staleItems: false,
    spaces,
  });
  const all = spaces.flatMap((space) => space.connections.map((connection) => ({ connection, space })));
  const open = all.find((row) => row.connection.id === openId);

  return (
    <SectionFrame
      ctx={ctx}
      state={state}
      actions={
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" asChild>
            <Link to="/connect" search={{ space: ctx.scope.spaceId ?? undefined }}>
              <Icon name="plus" size={20} />
              Подключить источник
            </Link>
          </Button>
          <Button variant="secondary" leadingIcon="document" onClick={() => setAdding(true)}>
            Добавить документ
          </Button>
        </div>
      }
    >
      {total === 0 ? (
        <EmptyState icon={ctx.def.icon} {...ctx.def.empty} />
      ) : (
        spaces.map((space) => (
          <SpaceConnections
            key={space.id}
            space={space}
            nowMs={nowMs}
            showTitle={spaces.length > 1}
            onOpen={setOpenId}
          />
        ))
      )}
      <ConnectionSheet
        connection={open?.connection ?? null}
        space={open?.space}
        nowMs={nowMs}
        onClose={() => setOpenId(null)}
      />
      <AddDocumentSheet
        open={adding}
        onOpenChange={setAdding}
        spaces={spaces}
        defaultSpaceId={ctx.scope.spaceId}
      />
    </SectionFrame>
  );
}

function SpaceConnections({
  space,
  nowMs,
  showTitle,
  onOpen,
}: {
  space: SpaceInfo;
  nowMs: number;
  showTitle: boolean;
  onOpen: (id: string) => void;
}) {
  if (space.connections.length === 0) return null;
  return (
    <section aria-labelledby={`connections-${space.id}`} className="flex flex-col gap-2">
      <h2 id={`connections-${space.id}`} className={showTitle ? "px-1 type-heading text-fg" : "sr-only"}>
        {space.title}
      </h2>
      <Card padding="sm">
        <ul>
          {space.connections.map((connection) => {
            const view = describeConnection(connection, nowMs, {
              shortTitle: space.shortTitle,
              timeZone: space.timeZone,
            });
            return (
              <li key={connection.id} data-connection-status={connection.status}>
                <ListRow
                  icon="connection"
                  title={connection.title}
                  meta={
                    <>
                      <span>
                        {connectionKindTitle(connection.kind)} · {originLabel(connection)}
                      </span>
                      <Badge tone={view.tone} icon={view.icon}>
                        {view.label}
                      </Badge>
                      <span>{view.detail}</span>
                    </>
                  }
                  onOpen={() => onOpen(connection.id)}
                />
              </li>
            );
          })}
        </ul>
      </Card>
    </section>
  );
}

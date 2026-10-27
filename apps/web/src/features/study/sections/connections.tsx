import { Badge, Card, displayHost, EmptyState, ListRow, safeHttpUrl, useNow } from "@botai/design";
import type { Connection } from "@/api/types";
import { connectionKindTitle, describeConnection } from "@/features/data/connection-status";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { type SectionContext, SectionFrame } from "../section-frame";
import { useSectionState } from "../use-section";

/** Адрес источника без схемы: «lk.fakeuni.test». Источник без адреса (документ) — только вид. */
export function originLabel(connection: Connection): string {
  const url = safeHttpUrl(connection.origin);
  return url ? displayHost(url) : connectionKindTitle(connection.kind);
}

/** «Подключения»: источники каждого вуза со статусом человеческим языком. Повторы и обновление идут сами. */
export function ConnectionsSection({ ctx }: { ctx: SectionContext }) {
  const nowMs = useNow(30_000);
  const spaces = ctx.scope.spaces;
  const total = spaces.reduce((sum, space) => sum + space.connections.length, 0);
  const state = useSectionState({
    hasData: ctx.directory.connectionsLoaded,
    isEmpty: total === 0,
    snapshot: false,
    failed: false,
    staleItems: false,
    spaces,
  });

  return (
    <SectionFrame ctx={ctx} state={state}>
      {total === 0 ? (
        <EmptyState icon={ctx.def.icon} {...ctx.def.empty} />
      ) : (
        spaces.map((space) => (
          <SpaceConnections key={space.id} space={space} nowMs={nowMs} showTitle={spaces.length > 1} />
        ))
      )}
    </SectionFrame>
  );
}

function SpaceConnections({
  space,
  nowMs,
  showTitle,
}: {
  space: SpaceInfo;
  nowMs: number;
  showTitle: boolean;
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
                />
              </li>
            );
          })}
        </ul>
      </Card>
    </section>
  );
}

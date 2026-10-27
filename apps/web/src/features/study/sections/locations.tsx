import { Badge, Card, countRu, formatWhen, ListRow, useNow } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { addDays, startOfDay } from "@/features/data/dates";
import { scheduleQuery } from "@/features/data/queries";
import { startMs } from "@/features/schedule/events";
import { ListSkeleton, RecordSheet, SectionEmpty, sourceFrom } from "../parts";
import { type SectionContext, SectionFrame } from "../section-frame";
import { isTrustworthy, useRecordsState } from "../use-section";
import { roomsFromEvents } from "./locations-model";

const WINDOW_DAYS = 28;
const PAIRS = ["пара", "пары", "пар"] as const;

/**
 * «Аудитории»: собираются из расписания на ближайшие четыре недели (отдельного списка аудиторий API не отдаёт).
 * Для каждой — ближайшая пара и сколько их впереди.
 */
export function LocationsSection({ ctx }: { ctx: SectionContext }) {
  const nowMs = useNow(60_000);
  const timeZone = ctx.scope.timeZone;
  const from = startOfDay(nowMs, timeZone);
  const query = useQuery(scheduleQuery(ctx.scope.spaceId, from, addDays(from, WINDOW_DAYS, timeZone)));
  const state = useRecordsState(query, ctx.scope.spaces);
  const [openRoom, setOpenRoom] = useState<string | null>(null);
  const rooms = useMemo(() => roomsFromEvents(query.data?.data ?? [], nowMs), [query.data, nowMs]);
  const showSpace = ctx.scope.isAll && ctx.directory.spaces.length > 1;
  const open = rooms.find((row) => row.room === openRoom) ?? null;
  const next = open?.upcoming[0];
  const zone = (spaceId: string) => ctx.directory.byId.get(spaceId)?.timeZone ?? timeZone;

  return (
    <SectionFrame ctx={ctx} state={state}>
      {state === "loading" ? (
        <ListSkeleton label="Загрузка аудиторий" />
      ) : rooms.length === 0 ? (
        <SectionEmpty icon={ctx.def.icon} {...ctx.def.empty} trustworthy={isTrustworthy(state)} />
      ) : (
        <>
          <p className="px-1 type-caption text-fg-muted">Собрано из расписания на ближайшие четыре недели.</p>
          <Card padding="sm">
            <ul>
              {rooms.map((info) => {
                const first = info.upcoming[0];
                return (
                  <li key={info.room}>
                    <ListRow
                      icon="room"
                      title={info.room}
                      meta={
                        first && (
                          <>
                            <span>
                              Ближайшая: «{first.title}»,{" "}
                              {formatWhen(startMs(first), nowMs, zone(first.provenance.spaceId))}
                            </span>
                            {showSpace && (
                              <Badge tone="neutral">
                                {ctx.directory.byId.get(first.provenance.spaceId)?.shortTitle}
                              </Badge>
                            )}
                          </>
                        )
                      }
                      trailing={countRu(info.upcoming.length, PAIRS)}
                      onOpen={() => setOpenRoom(info.room)}
                    />
                  </li>
                );
              })}
            </ul>
          </Card>
        </>
      )}
      <RecordSheet
        spaces={ctx.directory.byId}
        nowMs={nowMs}
        onClose={() => setOpenRoom(null)}
        record={
          open && next
            ? {
                title: open.room,
                subtitle: `Впереди ${countRu(open.upcoming.length, PAIRS)}`,
                rows: open.upcoming.slice(0, 8).map((event) => ({
                  icon: "schedule" as const,
                  label: formatWhen(startMs(event), nowMs, zone(event.provenance.spaceId)),
                  value: event.title,
                })),
                source: sourceFrom(next.provenance, ctx.directory.byId, "Расписание"),
              }
            : null
        }
      />
    </SectionFrame>
  );
}

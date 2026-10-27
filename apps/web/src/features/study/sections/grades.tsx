import { Badge, formatDay, GradeTile, useNow } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { gradesQuery } from "@/features/data/queries";
import { ListSkeleton, RecordSheet, SectionEmpty, sourceFrom } from "../parts";
import { type SectionContext, SectionFrame } from "../section-frame";
import { isTrustworthy, useRecordsState } from "../use-section";
import { groupGrades, weightLabel } from "./grades-model";

/** «Оценки»: плитки по предметам (две колонки от 430 px). Оценки показываются как их дал источник. */
export function GradesSection({ ctx }: { ctx: SectionContext }) {
  const nowMs = useNow(30_000);
  const query = useQuery(gradesQuery(ctx.scope.spaceId));
  const state = useRecordsState(query, ctx.scope.spaces);
  const [openId, setOpenId] = useState<string | null>(null);
  const items = query.data?.data ?? [];
  const groups = useMemo(() => groupGrades(items), [items]);
  const showSpace = ctx.scope.isAll && ctx.directory.spaces.length > 1;
  const open = items.find((row) => row.id === openId) ?? null;
  const timeZone = open ? ctx.directory.byId.get(open.provenance.spaceId)?.timeZone : undefined;

  return (
    <SectionFrame ctx={ctx} state={state}>
      {state === "loading" ? (
        <ListSkeleton label="Загрузка оценок" />
      ) : items.length === 0 ? (
        <SectionEmpty icon={ctx.def.icon} {...ctx.def.empty} trustworthy={isTrustworthy(state)} />
      ) : (
        groups.map((group, index) => (
          <section
            key={group.course ?? "none"}
            aria-labelledby={`grades-${index}`}
            className="flex flex-col gap-2"
          >
            <h2 id={`grades-${index}`} className="px-1 type-heading text-fg">
              {group.course ?? "Без предмета"}
            </h2>
            <ul className="grid grid-cols-1 gap-2 xs:grid-cols-2">
              {group.grades.map((grade) => (
                <li key={grade.id} className="min-w-0">
                  <GradeTile
                    value={grade.value}
                    scale={grade.scale}
                    title={grade.title}
                    meta={
                      <>
                        {weightLabel(grade.weight) && <span>{weightLabel(grade.weight)}</span>}
                        {grade.gradedAt && (
                          <time dateTime={grade.gradedAt}>
                            {formatDay(
                              Date.parse(grade.gradedAt),
                              nowMs,
                              ctx.directory.byId.get(grade.provenance.spaceId)?.timeZone,
                            )}
                          </time>
                        )}
                        {showSpace && (
                          <Badge tone="neutral">
                            {ctx.directory.byId.get(grade.provenance.spaceId)?.shortTitle}
                          </Badge>
                        )}
                      </>
                    }
                    onOpen={() => setOpenId(grade.id)}
                  />
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
      <RecordSheet
        spaces={ctx.directory.byId}
        nowMs={nowMs}
        onClose={() => setOpenId(null)}
        record={
          open && {
            title: open.title ?? `Оценка: ${open.value}`,
            subtitle: open.courseTitle,
            rows: [
              {
                icon: "grade",
                label: "Оценка",
                value: `${open.value}${open.scale ? ` · ${open.scale}` : ""}`,
              },
              ...(weightLabel(open.weight)
                ? [{ icon: "info" as const, label: "Вес", value: weightLabel(open.weight) }]
                : []),
              ...(open.gradedAt
                ? [
                    {
                      icon: "time" as const,
                      label: "Выставлена",
                      value: formatDay(Date.parse(open.gradedAt), nowMs, timeZone),
                    },
                  ]
                : []),
            ],
            source: sourceFrom(open.provenance, ctx.directory.byId, "Оценки"),
          }
        }
      />
    </SectionFrame>
  );
}

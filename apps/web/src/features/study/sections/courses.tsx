import { Badge, Card, ListRow, safeHttpUrl, useNow } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { coursesQuery } from "@/features/data/queries";
import { ExternalLinkButton, ListSkeleton, RecordSheet, SectionEmpty, sourceFrom } from "../parts";
import { type SectionContext, SectionFrame } from "../section-frame";
import { isTrustworthy, useRecordsState } from "../use-section";

/** «Предметы»: список предметов вузов с кодом и периодом; карточка — источник и ссылка на страницу предмета. */
export function CoursesSection({ ctx }: { ctx: SectionContext }) {
  const nowMs = useNow(30_000);
  const query = useQuery(coursesQuery(ctx.scope.spaceId));
  const state = useRecordsState(query, ctx.scope.spaces);
  const [openId, setOpenId] = useState<string | null>(null);
  const items = query.data?.data ?? [];
  const showSpace = ctx.scope.isAll && ctx.directory.spaces.length > 1;
  const open = items.find((row) => row.id === openId) ?? null;
  const link = safeHttpUrl(open?.url);

  return (
    <SectionFrame ctx={ctx} state={state}>
      {state === "loading" ? (
        <ListSkeleton label="Загрузка предметов" />
      ) : items.length === 0 ? (
        <SectionEmpty icon={ctx.def.icon} {...ctx.def.empty} trustworthy={isTrustworthy(state)} />
      ) : (
        <Card padding="sm">
          <ul>
            {items.map((course) => (
              <li key={course.id}>
                <ListRow
                  icon="course"
                  title={course.title}
                  meta={
                    <>
                      {[course.code, course.term].filter(Boolean).join(" · ") && (
                        <span>{[course.code, course.term].filter(Boolean).join(" · ")}</span>
                      )}
                      {showSpace && (
                        <Badge tone="neutral">
                          {ctx.directory.byId.get(course.provenance.spaceId)?.shortTitle}
                        </Badge>
                      )}
                    </>
                  }
                  onOpen={() => setOpenId(course.id)}
                />
              </li>
            ))}
          </ul>
        </Card>
      )}
      <RecordSheet
        spaces={ctx.directory.byId}
        nowMs={nowMs}
        onClose={() => setOpenId(null)}
        record={
          open && {
            title: open.title,
            subtitle: [open.code, open.term].filter(Boolean).join(" · ") || undefined,
            rows: [
              ...(open.code ? [{ icon: "info" as const, label: "Код", value: open.code }] : []),
              ...(open.term ? [{ icon: "time" as const, label: "Период", value: open.term }] : []),
            ],
            source: sourceFrom(open.provenance, ctx.directory.byId, "Предметы"),
            actions: link ? (
              <ExternalLinkButton href={link.href}>Открыть предмет</ExternalLinkButton>
            ) : undefined,
          }
        }
      />
    </SectionFrame>
  );
}

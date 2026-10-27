import { Badge, Button, Card, ListRow, useNow } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { peopleQuery } from "@/features/data/queries";
import { ListSkeleton, RecordSheet, SectionEmpty, sourceFrom } from "../parts";
import { type SectionContext, SectionFrame } from "../section-frame";
import { isTrustworthy, useRecordsState } from "../use-section";

/** Почта показывается ссылкой «Написать», только если выглядит как адрес: текст из вуза — данные, не разметка. */
const EMAIL = /^[^\s@<>"']+@[^\s@<>"']+\.[^\s@<>"']+$/;

/** «Преподаватели»: имя, роль, почта; карточка — источник и «Написать». */
export function PeopleSection({ ctx }: { ctx: SectionContext }) {
  const nowMs = useNow(30_000);
  const query = useQuery(peopleQuery(ctx.scope.spaceId));
  const state = useRecordsState(query, ctx.scope.spaces);
  const [openId, setOpenId] = useState<string | null>(null);
  const items = query.data?.data ?? [];
  const showSpace = ctx.scope.isAll && ctx.directory.spaces.length > 1;
  const open = items.find((row) => row.id === openId) ?? null;
  const email = open?.email && EMAIL.test(open.email) ? open.email : null;

  return (
    <SectionFrame ctx={ctx} state={state}>
      {state === "loading" ? (
        <ListSkeleton label="Загрузка преподавателей" />
      ) : items.length === 0 ? (
        <SectionEmpty icon={ctx.def.icon} {...ctx.def.empty} trustworthy={isTrustworthy(state)} />
      ) : (
        <Card padding="sm">
          <ul>
            {items.map((person) => (
              <li key={person.id}>
                <ListRow
                  icon="person"
                  title={person.name}
                  meta={
                    <>
                      <span>{person.role}</span>
                      {showSpace && (
                        <Badge tone="neutral">
                          {ctx.directory.byId.get(person.provenance.spaceId)?.shortTitle}
                        </Badge>
                      )}
                    </>
                  }
                  onOpen={() => setOpenId(person.id)}
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
            title: open.name,
            subtitle: open.role,
            rows: open.email ? [{ icon: "info", label: "Почта", value: open.email }] : [],
            source: sourceFrom(open.provenance, ctx.directory.byId, "Преподаватели"),
            actions: email ? (
              <Button variant="secondary" asChild>
                <a href={`mailto:${encodeURI(email)}`}>Написать</a>
              </Button>
            ) : undefined,
          }
        }
      />
    </SectionFrame>
  );
}

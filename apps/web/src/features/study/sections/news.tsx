import { Badge, Card, formatDay, ListRow, safeHttpUrl, useNow } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { newsQuery } from "@/features/data/queries";
import { ExternalLinkButton, ListSkeleton, RecordSheet, SectionEmpty, sourceFrom } from "../parts";
import { type SectionContext, SectionFrame } from "../section-frame";
import { isTrustworthy, useRecordsState } from "../use-section";

/** «Новости»: свежие первыми. Текст новости — обычный текст источника, ссылка на оригинал открывается отдельно. */
export function NewsSection({ ctx }: { ctx: SectionContext }) {
  const nowMs = useNow(30_000);
  const query = useQuery(newsQuery(ctx.scope.spaceId));
  const state = useRecordsState(query, ctx.scope.spaces);
  const [openId, setOpenId] = useState<string | null>(null);
  const items = query.data?.data ?? [];
  const showSpace = ctx.scope.isAll && ctx.directory.spaces.length > 1;
  const open = items.find((row) => row.id === openId) ?? null;
  const link = safeHttpUrl(open?.url);
  const day = (iso: string, spaceId: string) =>
    formatDay(Date.parse(iso), nowMs, ctx.directory.byId.get(spaceId)?.timeZone);

  return (
    <SectionFrame ctx={ctx} state={state}>
      {state === "loading" ? (
        <ListSkeleton label="Загрузка новостей" />
      ) : items.length === 0 ? (
        <SectionEmpty icon={ctx.def.icon} {...ctx.def.empty} trustworthy={isTrustworthy(state)} />
      ) : (
        <Card padding="sm">
          <ul>
            {items.map((item) => (
              <li key={item.id}>
                <ListRow
                  icon="news"
                  title={item.title}
                  meta={
                    <>
                      <time dateTime={item.publishedAt}>
                        {day(item.publishedAt, item.provenance.spaceId)}
                      </time>
                      {showSpace && (
                        <Badge tone="neutral">
                          {ctx.directory.byId.get(item.provenance.spaceId)?.shortTitle}
                        </Badge>
                      )}
                    </>
                  }
                  onOpen={() => setOpenId(item.id)}
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
            subtitle: day(open.publishedAt, open.provenance.spaceId),
            body: open.body,
            source: sourceFrom(open.provenance, ctx.directory.byId, "Новости"),
            actions: link ? (
              <ExternalLinkButton href={link.href}>Открыть новость</ExternalLinkButton>
            ) : undefined,
          }
        }
      />
    </SectionFrame>
  );
}

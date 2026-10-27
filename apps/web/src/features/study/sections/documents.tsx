import {
  Badge,
  Button,
  Card,
  Dialog,
  displayHost,
  formatAgo,
  formatDay,
  formatTime,
  ListRow,
  safeHttpUrl,
  toast,
  useNow,
} from "@botai/design";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { humanMessage } from "@/api/errors";
import { documentsQuery } from "@/features/data/queries";
import type { DocumentRow } from "@/features/data/types";
import { AddDocumentSheet } from "../add-document-sheet";
import { deleteDocument, refreshAfterChange } from "../connection-actions";
import { ExternalLinkButton, ListSkeleton, RecordSheet, SectionEmpty } from "../parts";
import { type SectionContext, SectionFrame } from "../section-frame";
import { isTrustworthy, useSectionState } from "../use-section";

export const DOCUMENT_KIND_LABEL: Record<DocumentRow["kind"], string> = {
  file: "Файл",
  sheet: "Таблица",
  page: "Страница",
};

interface Combined {
  items: DocumentRow[];
  hasData: boolean;
  snapshot: boolean;
  failed: boolean;
}

const combine = (
  results: readonly {
    data?: { data: DocumentRow[]; snapshotAt: number | null };
    isError: boolean;
  }[],
): Combined => ({
  items: results.flatMap((result) => result.data?.data ?? []),
  hasData: results.length > 0 && results.every((result) => result.data !== undefined),
  snapshot: results.some((result) => result.data?.snapshotAt != null),
  failed: results.some((result) => result.isError),
});

/** «Документы»: ссылки и файлы, которые BotAI прочитал. Свежие первыми; у каждого — вуз и время получения. */
export function DocumentsSection({ ctx }: { ctx: SectionContext }) {
  const nowMs = useNow(30_000);
  const combined = useQueries({
    queries: ctx.scope.spaces.map((space) => documentsQuery(space.id)),
    combine,
  });
  const items = [...combined.items].sort((a, b) => b.fetchedAt.localeCompare(a.fetchedAt));
  const state = useSectionState({
    hasData: combined.hasData,
    isEmpty: items.length === 0,
    snapshot: combined.snapshot,
    failed: combined.failed,
    staleItems: false,
    spaces: ctx.scope.spaces,
  });
  const queryClient = useQueryClient();
  const [openId, setOpenId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<DocumentRow | null>(null);
  const showSpace = ctx.scope.isAll && ctx.directory.spaces.length > 1;
  const open = items.find((row) => row.id === openId) ?? null;
  const space = open ? ctx.directory.byId.get(open.spaceId) : undefined;
  const link = safeHttpUrl(open?.url);

  return (
    <SectionFrame
      ctx={ctx}
      state={state}
      actions={
        <Button variant="secondary" leadingIcon="plus" className="self-start" onClick={() => setAdding(true)}>
          Добавить документ
        </Button>
      }
    >
      {state === "loading" ? (
        <ListSkeleton label="Загрузка документов" />
      ) : items.length === 0 ? (
        <SectionEmpty icon={ctx.def.icon} {...ctx.def.empty} trustworthy={isTrustworthy(state)} />
      ) : (
        <Card padding="sm">
          <ul>
            {items.map((document) => {
              const zone = ctx.directory.byId.get(document.spaceId)?.timeZone;
              return (
                <li key={document.id}>
                  <ListRow
                    icon="document"
                    title={document.title}
                    meta={
                      <>
                        <span>{DOCUMENT_KIND_LABEL[document.kind]}</span>
                        <time dateTime={document.fetchedAt}>
                          Получен {formatAgo(Date.parse(document.fetchedAt), nowMs, zone)}
                        </time>
                        {showSpace && (
                          <Badge tone="neutral">{ctx.directory.byId.get(document.spaceId)?.shortTitle}</Badge>
                        )}
                      </>
                    }
                    onOpen={() => setOpenId(document.id)}
                  />
                </li>
              );
            })}
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
            subtitle: DOCUMENT_KIND_LABEL[open.kind],
            rows: [
              ...(link ? [{ icon: "source" as const, label: "Адрес", value: displayHost(link) }] : []),
              {
                icon: "time" as const,
                label: "Получен",
                value: `${formatDay(Date.parse(open.fetchedAt), nowMs, space?.timeZone)}, ${formatTime(Date.parse(open.fetchedAt), space?.timeZone)}`,
              },
            ],
            source: {
              title: open.title,
              url: open.url,
              fetchedAt: Date.parse(open.fetchedAt),
              spaceId: open.spaceId,
            },
            actions: (
              <div className="flex flex-col gap-2">
                {link && <ExternalLinkButton href={link.href}>Открыть документ</ExternalLinkButton>}
                <Button
                  variant="secondary"
                  tone="danger"
                  leadingIcon="trash"
                  onClick={() => setDeleting(open)}
                >
                  Удалить документ
                </Button>
              </div>
            ),
          }
        }
      />
      <AddDocumentSheet
        open={adding}
        onOpenChange={setAdding}
        spaces={ctx.scope.spaces}
        defaultSpaceId={ctx.scope.spaceId}
      />
      <Dialog
        open={deleting !== null}
        onOpenChange={(next) => !next && setDeleting(null)}
        title="Удалить документ?"
        description={`«${deleting?.title ?? ""}» исчезнет из BotAI и из ответов чата. Сам файл или страница у источника останутся.`}
        confirmLabel="Удалить документ"
        onConfirm={async () => {
          const target = deleting;
          if (!target) return;
          try {
            await deleteDocument(target.id);
            toast("Документ удалён", { tone: "success" });
            setOpenId(null);
            await refreshAfterChange(queryClient, target.spaceId);
          } catch (cause) {
            toast("Не удалось удалить документ", { tone: "warning", description: humanMessage(cause) });
          }
        }}
      />
    </SectionFrame>
  );
}

import { Button, Card, EmptyState, Icon, useNow } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import type { Connection } from "@/api/types";
import { addCalendarLink, addSourceLink, connectUniversityLink } from "@/features/data/chat-links";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { AddDocumentSheet } from "../add-document-sheet";
import { ConnectionSheet } from "../connection-sheet";
import { type SectionContext, SectionFrame } from "../section-frame";
import { SourceList } from "../source-list";
import { useSectionState } from "../use-section";

/**
 * «Источники»: все источники выбранных вузов с управлением. Нажатие открывает карточку: ссылка календаря или
 * ключ, «Забыть пароль», удаление. «Добавить источник» (и его вариант «Календарь (iCal)») и «Подключить ещё вуз» открывают чат с готовой просьбой,
 * здесь же — «Добавить документ». Обновление и повторы идут сами, кнопки «Обновить» нет.
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
            <Link {...connectUniversityLink()}>
              <Icon name="plus" size={20} />
              Подключить ещё вуз
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
          <SpaceSources
            key={space.id}
            space={space}
            nowMs={nowMs}
            showTitle={spaces.length > 1}
            onOpen={(connection) => setOpenId(connection.id)}
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

function SpaceSources({
  space,
  nowMs,
  showTitle,
  onOpen,
}: {
  space: SpaceInfo;
  nowMs: number;
  showTitle: boolean;
  onOpen: (connection: Connection) => void;
}) {
  if (space.connections.length === 0) return null;
  return (
    <section aria-labelledby={`connections-${space.id}`} className="flex flex-col gap-2">
      <h2 id={`connections-${space.id}`} className={showTitle ? "px-1 type-heading text-fg" : "sr-only"}>
        {space.title}
      </h2>
      <Card padding="sm" className="flex flex-col">
        <SourceList space={space} nowMs={nowMs} onOpen={onOpen} />
        <div className="flex flex-wrap gap-x-2">
          <Button variant="ghost" asChild>
            <Link {...addSourceLink(space.title)}>
              <Icon name="plus" size={20} />
              Добавить источник
            </Link>
          </Button>
          <Button variant="ghost" asChild>
            <Link {...addCalendarLink(space.title)}>
              <Icon name="schedule" size={20} />
              Календарь (iCal)
            </Link>
          </Button>
        </div>
      </Card>
    </section>
  );
}

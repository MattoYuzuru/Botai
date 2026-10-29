import {
  Badge,
  Button,
  formatWhen,
  Icon,
  Sheet,
  SourceCitation,
  safeHttpUrl,
  urgencyLabel,
} from "@botai/design";
import { useRef } from "react";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import { openUrlOf } from "@/features/data/open-url";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { DEADLINE_STATE_LABEL, type DeadlineView } from "./deadlines";

export interface DeadlineSheetProps {
  /** Выбранное задание; `null` — шит закрыт. Содержимое остаётся на время анимации закрытия. */
  view: DeadlineView | null;
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
  onClose: () => void;
}

/**
 * Карточка задания: срок (честное «Срок не указан», если его нет), статус, предмет, источник со временем
 * получения и ссылка на задание в источнике. Условие задания сервер не отдаёт — оно по ссылке.
 */
export function DeadlineSheet({ view, spaces, nowMs, onClose }: DeadlineSheetProps) {
  const last = useRef<DeadlineView | null>(null);
  if (view) last.current = view;
  const shown = view ?? last.current;
  return (
    <Sheet
      open={view !== null}
      onOpenChange={(open) => !open && onClose()}
      title={shown?.deadline.title ?? "Задание"}
      description={shown?.deadline.courseTitle ?? undefined}
    >
      {shown && <DeadlineDetails view={shown} spaces={spaces} nowMs={nowMs} />}
    </Sheet>
  );
}

function DeadlineDetails({
  view,
  spaces,
  nowMs,
}: {
  view: DeadlineView;
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
}) {
  const { deadline, dueMs, urgency, done } = view;
  const space = spaces.get(deadline.provenance.spaceId);
  const timeZone = space?.timeZone;
  const label = urgencyLabel(urgency, dueMs, nowMs, timeZone ?? "Europe/Moscow");
  const connection = space?.connections.find((row) => row.id === deadline.provenance.connectionId);
  const link = safeHttpUrl(deadline.url);
  const baseDue = deadline.dueAt ? Date.parse(deadline.dueAt) : null;
  const moved = baseDue !== null && dueMs !== null && baseDue !== dueMs;

  return (
    <div className="flex flex-col gap-4 pb-2">
      <div className="flex flex-wrap gap-1.5">
        {space && <Badge tone="neutral">{space.shortTitle}</Badge>}
        {done ? (
          <Badge tone="success" icon="ok">
            {DEADLINE_STATE_LABEL[deadline.state]}
          </Badge>
        ) : (
          <>
            {urgency === "urgent" && label.badge && (
              <Badge tone="warning" variant="solid">
                {label.badge}
              </Badge>
            )}
            {urgency === "soon" && label.badge && <Badge tone="warning">{label.badge}</Badge>}
            {urgency === "overdue" && <Badge tone="danger">{label.badge}</Badge>}
            {deadline.state === "missed" && <Badge tone="danger">{DEADLINE_STATE_LABEL.missed}</Badge>}
          </>
        )}
      </div>

      <DetailList label="Подробности задания">
        <DetailRow icon="time" label="Срок">
          {label.due}
        </DetailRow>
        {moved && baseDue !== null && (
          <DetailRow icon="info" label="Срок по плану курса">
            {formatWhen(baseDue, nowMs, timeZone)}
          </DetailRow>
        )}
        <DetailRow icon="deadline" label="Статус">
          {DEADLINE_STATE_LABEL[deadline.state]}
        </DetailRow>
        <DetailRow icon="course" label="Предмет">
          {deadline.courseTitle}
        </DetailRow>
      </DetailList>

      {deadline.description && (
        <section aria-label="Условие задания" className="flex flex-col gap-1.5">
          <h3 className="type-label text-fg-muted">Условие</h3>
          <p className="type-body whitespace-pre-line text-fg">{deadline.description}</p>
        </section>
      )}

      <SourceCitation
        title={connection?.title ?? "Задания"}
        url={openUrlOf(null, deadline.provenance, connection)}
        fetchedAt={Date.parse(deadline.provenance.fetchedAt)}
        nowMs={nowMs}
        timeZone={timeZone}
        spaceId={deadline.provenance.spaceId}
        spaceTitle={space?.shortTitle}
        stale={deadline.provenance.stale}
      />

      {link ? (
        <div className="flex flex-col gap-2">
          <Button variant="secondary" asChild>
            <a href={link.href} target="_blank" rel="noopener noreferrer">
              <Icon name="external" size={20} />
              Открыть задание
            </a>
          </Button>
          <p className="type-caption text-fg-muted">
            Условие задания лежит в источнике и открывается по ссылке.
          </p>
        </div>
      ) : (
        <p className="type-caption text-fg-muted">Ссылки на задание в источнике нет.</p>
      )}
    </div>
  );
}

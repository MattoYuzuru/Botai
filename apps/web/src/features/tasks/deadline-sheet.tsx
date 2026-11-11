import {
  Badge,
  Button,
  formatWhen,
  Icon,
  ScoreBar,
  Sheet,
  SourceCitation,
  safeHttpUrl,
  urgencyLabel,
} from "@botai/design";
import { useRef } from "react";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import { openUrlOf } from "@/features/data/open-url";
import { fillOf } from "@/features/data/scores";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { DEADLINE_STATE_LABEL, type DeadlineView, deadlineScore, deadlineStatus } from "./deadlines";
import { useMarkDeadline } from "./mark";

export interface DeadlineSheetProps {
  /** Выбранное задание; `null` — шит закрыт. Содержимое остаётся на время анимации закрытия. */
  view: DeadlineView | null;
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
  onClose: () => void;
}

/**
 * Карточка задания: срок (честное «Срок не указан», если его нет), статус словами источника и откуда он, балл
 * проверенной работы, предмет, источник со временем получения, «Открыть задание» (страница задания, иначе
 * страница источника) и отметка студента «Отметить сданным» / «Вернуть в работу» (D-045).
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
  const sourcePage = openUrlOf(deadline.provenance, connection);
  const link = safeHttpUrl(deadline.url)?.href ?? sourcePage;
  const baseDue = deadline.dueAt ? Date.parse(deadline.dueAt) : null;
  const moved = baseDue !== null && dueMs !== null && baseDue !== dueMs;
  const status = deadlineStatus(deadline);
  const score = deadlineScore(deadline);
  const scoreFill = deadline.grade ? fillOf(deadline.grade) : null;
  const gradeLink = safeHttpUrl(deadline.grade?.url)?.href ?? null;
  const marked = deadline.stateSource === "student";
  const mark = useMarkDeadline();
  const markPending = mark.isPending && mark.variables?.id === deadline.id;

  return (
    <div className="flex flex-col gap-4 pb-2">
      <div className="flex flex-wrap gap-1.5">
        {space && <Badge tone="neutral">{space.shortTitle}</Badge>}
        {done ? (
          <Badge tone="success" icon="ok">
            {status.text}
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
            {deadline.stateText?.trim() && !marked && <Badge tone="neutral">{status.text}</Badge>}
          </>
        )}
        {status.note && (
          <Badge tone="neutral" variant="outline" icon="edit">
            {status.note}
          </Badge>
        )}
      </div>

      {score && (
        <section
          aria-label="Оценка за задание"
          className="flex flex-col gap-2 rounded-lg border border-border p-3"
        >
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="type-label text-fg-muted">Оценка</h3>
            <p className="type-title text-fg" data-numeric="">
              {score}
            </p>
          </div>
          {scoreFill !== null && <ScoreBar fill={scoreFill} />}
          {gradeLink && (
            <Button variant="ghost" asChild className="-ml-3 self-start">
              <a href={gradeLink} target="_blank" rel="noopener noreferrer">
                <Icon name="external" size={20} />
                Открыть оценку в источнике
              </a>
            </Button>
          )}
        </section>
      )}

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
          {status.note ? `${status.text} · ${status.note}` : status.text}
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
        url={openUrlOf(deadline.provenance, connection)}
        fetchedAt={Date.parse(deadline.provenance.fetchedAt)}
        nowMs={nowMs}
        timeZone={timeZone}
        spaceId={deadline.provenance.spaceId}
        spaceTitle={space?.shortTitle}
        stale={deadline.provenance.stale}
      />

      <div className="flex flex-col gap-2">
        {link ? (
          <Button variant="primary" asChild>
            <a href={link} target="_blank" rel="noopener noreferrer">
              <Icon name="external" size={20} />
              Открыть задание
            </a>
          </Button>
        ) : (
          <p className="type-caption text-fg-muted">Ссылки на задание в источнике нет.</p>
        )}
        {!done ? (
          <Button
            variant="secondary"
            leadingIcon="ok"
            loading={markPending}
            onClick={() => mark.mutate({ id: deadline.id, userState: "submitted" })}
          >
            Отметить сданным
          </Button>
        ) : (
          (marked || deadline.state === "submitted") && (
            <Button
              variant="secondary"
              leadingIcon="history"
              loading={markPending}
              onClick={() => mark.mutate({ id: deadline.id, userState: "open" })}
            >
              Вернуть в работу
            </Button>
          )
        )}
        {marked && (
          <Button
            variant="ghost"
            loading={markPending}
            onClick={() => mark.mutate({ id: deadline.id, userState: null })}
          >
            Снять мою отметку
          </Button>
        )}
        <p className="type-caption text-fg-muted">
          {marked
            ? "Статус отмечен вами и сильнее источника, пока вы его не снимете."
            : link === sourcePage && !deadline.url
              ? "Отдельной страницы задания источник не дал: откроется страница источника."
              : "Условие задания лежит в источнике и открывается по ссылке."}
        </p>
      </div>
    </div>
  );
}

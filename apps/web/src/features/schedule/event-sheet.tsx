import {
  Badge,
  Banner,
  Button,
  formatDayWithWeekday,
  formatTime,
  Icon,
  Sheet,
  SourceCitation,
  safeHttpUrl,
} from "@botai/design";
import { useRef } from "react";
import type { ScheduleEvent } from "@/api/types";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { EVENT_FORMAT_LABEL, EVENT_KIND_LABEL, endMs, startMs } from "./events";

export interface EventSheetProps {
  /** Выбранная пара; `null` — шит закрыт. Содержимое остаётся на время анимации закрытия. */
  event: ScheduleEvent | null;
  /** Пары, среди которых ищутся участники пересечения. */
  events: readonly ScheduleEvent[];
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
  onClose: () => void;
}

/**
 * Карточка пары: время, аудитория, преподаватель, предмет, пересечение с парой другого вуза и источник со
 * временем получения. На ноутбуке шит — правая панель.
 */
export function EventSheet({ event, events, spaces, nowMs, onClose }: EventSheetProps) {
  const last = useRef<ScheduleEvent | null>(null);
  if (event) last.current = event;
  const shown = event ?? last.current;
  return (
    <Sheet
      open={event !== null}
      onOpenChange={(open) => !open && onClose()}
      title={shown?.title ?? "Пара"}
      description={shown ? describeWhen(shown, spaces, nowMs) : undefined}
    >
      {shown && <EventDetails event={shown} events={events} spaces={spaces} nowMs={nowMs} />}
    </Sheet>
  );
}

function describeWhen(event: ScheduleEvent, spaces: ReadonlyMap<string, SpaceInfo>, nowMs: number): string {
  const timeZone = spaces.get(event.provenance.spaceId)?.timeZone;
  const start = startMs(event);
  return `${formatDayWithWeekday(start, nowMs, timeZone)}, ${formatTime(start, timeZone)}–${formatTime(endMs(event), timeZone)}`;
}

function EventDetails({
  event,
  events,
  spaces,
  nowMs,
}: {
  event: ScheduleEvent;
  events: readonly ScheduleEvent[];
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
}) {
  const space = spaces.get(event.provenance.spaceId);
  const timeZone = space?.timeZone;
  const kind = EVENT_KIND_LABEL[event.kind];
  const format = EVENT_FORMAT_LABEL[event.format];
  const connection = space?.connections.find((row) => row.id === event.provenance.connectionId);
  const link = safeHttpUrl(event.url);
  const partners = event.conflictsWith.map((id) => events.find((row) => row.id === id) ?? null);

  return (
    <div className="flex flex-col gap-4 pb-2">
      <div className="flex flex-wrap gap-1.5">
        {space && <Badge tone="neutral">{space.shortTitle}</Badge>}
        {kind && <Badge tone="info">{kind}</Badge>}
        {format && <Badge tone="neutral">{format}</Badge>}
        {event.changed && (
          <Badge tone="warning" icon="edit">
            Изменено
          </Badge>
        )}
      </div>

      {partners.length > 0 && (
        <Banner tone="warning" icon="warning">
          <span className="flex flex-col gap-1">
            <span className="type-label">Пересекается по времени</span>
            {partners.map((partner, index) => (
              <span key={partner?.id ?? `unknown-${event.conflictsWith[index]}`} className="type-ui">
                {partner
                  ? `«${partner.title}», ${spaces.get(partner.provenance.spaceId)?.shortTitle ?? "другой вуз"}, ${formatTime(startMs(partner), timeZone)}–${formatTime(endMs(partner), timeZone)}`
                  : "Пара из другого вуза"}
              </span>
            ))}
          </span>
        </Banner>
      )}

      <DetailList label="Подробности пары">
        <DetailRow icon="room" label="Аудитория">
          {event.location}
        </DetailRow>
        <DetailRow icon="person" label="Преподаватель">
          {event.teacher}
        </DetailRow>
        <DetailRow icon="course" label="Предмет">
          {event.courseTitle}
        </DetailRow>
      </DetailList>

      <SourceCitation
        title={connection?.title ?? "Расписание"}
        url={event.provenance.sourceUrl}
        fetchedAt={Date.parse(event.provenance.fetchedAt)}
        nowMs={nowMs}
        timeZone={timeZone}
        spaceId={event.provenance.spaceId}
        spaceTitle={space?.shortTitle}
        stale={event.provenance.stale}
      />

      {link && (
        <Button variant="secondary" asChild>
          <a href={link.href} target="_blank" rel="noopener noreferrer">
            <Icon name="external" size={20} />
            Открыть в личном кабинете
          </a>
        </Button>
      )}
    </div>
  );
}

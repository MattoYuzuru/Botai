import {
  Badge,
  Banner,
  Button,
  formatDayWithWeekday,
  formatTime,
  Icon,
  Sheet,
  SourceCitation,
} from "@botai/design";
import { useRef } from "react";
import type { ScheduleEvent } from "@/api/types";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import { openUrlOf } from "@/features/data/open-url";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { EVENT_FORMAT_LABEL, eventDetails } from "./event-details";
import { endMs, startMs } from "./events";

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
 * Карточка пары по пресету `eventDetails` (event-details.ts): тип занятия бейджем, время, номер пары, аудитория,
 * адрес, преподаватель, группа, предмет и прочие атрибуты источника — только те, что есть, каждый со своей
 * иконкой; онлайн-ссылки кнопками; пересечение с парой другого вуза; источник со временем получения.
 * «Открыть в личном кабинете» ведёт на страницу интерфейса источника, а не на адрес API. На ноутбуке шит —
 * правая панель.
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
      description={shown ? describeDay(shown, spaces, nowMs) : undefined}
    >
      {shown && <EventDetails event={shown} events={events} spaces={spaces} nowMs={nowMs} />}
    </Sheet>
  );
}

function describeDay(event: ScheduleEvent, spaces: ReadonlyMap<string, SpaceInfo>, nowMs: number): string {
  const timeZone = spaces.get(event.provenance.spaceId)?.timeZone;
  return formatDayWithWeekday(startMs(event), nowMs, timeZone);
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
  const details = eventDetails(event, { timeZone });
  const format = EVENT_FORMAT_LABEL[event.format];
  const connection = space?.connections.find((row) => row.id === event.provenance.connectionId);
  const openUrl = openUrlOf(event, event.provenance, connection);
  const partners = event.conflictsWith.map((id) => events.find((row) => row.id === id) ?? null);

  return (
    <div className="flex flex-col gap-4 pb-2">
      <div className="flex flex-wrap gap-1.5">
        {space && <Badge tone="neutral">{space.shortTitle}</Badge>}
        {details.badge && <Badge tone="info">{details.badge}</Badge>}
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
        {details.facts.map((fact) => (
          <DetailRow key={fact.key} icon={fact.icon} label={fact.label}>
            {fact.text}
          </DetailRow>
        ))}
      </DetailList>

      {details.links.length > 0 && (
        <section aria-label="Онлайн-ссылки" className="flex flex-col gap-2">
          {details.links.map((link) => (
            <Button key={link.href} variant="secondary" asChild>
              <a href={link.href} target="_blank" rel="noopener noreferrer">
                <Icon name="external" size={20} />
                <span className="min-w-0 truncate">{link.label}</span>
              </a>
            </Button>
          ))}
        </section>
      )}

      <SourceCitation
        title={connection?.title ?? "Расписание"}
        url={openUrl}
        fetchedAt={Date.parse(event.provenance.fetchedAt)}
        nowMs={nowMs}
        timeZone={timeZone}
        spaceId={event.provenance.spaceId}
        spaceTitle={space?.shortTitle}
        stale={event.provenance.stale}
      />

      {openUrl && (
        <Button variant="secondary" asChild>
          <a href={openUrl} target="_blank" rel="noopener noreferrer">
            <Icon name="external" size={20} />
            Открыть в личном кабинете
          </a>
        </Button>
      )}
    </div>
  );
}

import {
  Badge,
  Button,
  EmptyState,
  Icon,
  type IconName,
  Sheet,
  Skeleton,
  SourceCitation,
} from "@botai/design";
import { type ReactNode, useRef } from "react";
import type { Provenance } from "@/api/types";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import { openUrlOf } from "@/features/data/open-url";
import type { SpaceInfo } from "@/features/data/use-space-directory";

/** Скелетон списка записей: форма будущих строк, `aria-busy`, без спиннера. */
export function ListSkeleton({ rows = 4, label = "Загрузка" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-busy="true" className="flex flex-col gap-2">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: строки скелетона статичны
        <Skeleton key={index} className="h-14 rounded-lg" />
      ))}
    </div>
  );
}

export interface RecordView {
  title: string;
  subtitle?: string | null;
  badges?: ReactNode;
  rows?: { icon: IconName; label: string; value: ReactNode }[];
  /** Длинный текст источника: показывается обычным текстом, разметка не исполняется. */
  body?: string | null;
  notice?: ReactNode;
  source?: {
    title: string;
    url: string | null;
    fetchedAt: number;
    stale?: boolean;
    spaceId: string;
  };
  actions?: ReactNode;
}

/**
 * Карточка записи раздела (предмет, оценка, преподаватель, новость, документ): свойства, текст источника как
 * текст и `SourceCitation` со временем получения. Содержимое остаётся на время анимации закрытия.
 */
export function RecordSheet({
  record,
  spaces,
  nowMs,
  onClose,
  footer,
}: {
  record: RecordView | null;
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
  onClose: () => void;
  footer?: ReactNode;
}) {
  const last = useRef<RecordView | null>(null);
  if (record) last.current = record;
  const shown = record ?? last.current;
  const space = shown?.source ? spaces.get(shown.source.spaceId) : undefined;
  return (
    <Sheet
      open={record !== null}
      onOpenChange={(open) => !open && onClose()}
      title={shown?.title ?? "Запись"}
      description={shown?.subtitle ?? undefined}
      footer={footer}
    >
      {shown && (
        <div className="flex flex-col gap-4 pb-2">
          {(shown.badges || space) && (
            <div className="flex flex-wrap gap-1.5">
              {space && <Badge tone="neutral">{space.shortTitle}</Badge>}
              {shown.badges}
            </div>
          )}
          {shown.notice}
          {shown.rows && shown.rows.length > 0 && (
            <DetailList label="Подробности">
              {shown.rows.map((row) => (
                <DetailRow key={row.label} icon={row.icon} label={row.label}>
                  {row.value}
                </DetailRow>
              ))}
            </DetailList>
          )}
          {shown.body && <p className="whitespace-pre-line type-body text-fg">{shown.body}</p>}
          {shown.source && (
            <SourceCitation
              title={shown.source.title}
              url={shown.source.url}
              fetchedAt={shown.source.fetchedAt}
              nowMs={nowMs}
              timeZone={space?.timeZone}
              spaceId={shown.source.spaceId}
              spaceTitle={space?.shortTitle}
              stale={shown.source.stale}
            />
          )}
          {shown.actions}
        </div>
      )}
    </Sheet>
  );
}

/** Пустой раздел или данные, которым нельзя верить как «пусто»: слова зависят от состояния данных. */
export function SectionEmpty({
  icon,
  title,
  description,
  trustworthy,
  action,
}: {
  icon: IconName;
  title: string;
  description: string;
  /** Данные свежие: пустота настоящая. Иначе объясняем, что данные могут быть неполными. */
  trustworthy: boolean;
  action?: ReactNode;
}) {
  return (
    <EmptyState
      icon={icon}
      title={title}
      description={
        trustworthy
          ? description
          : "В этих данных ничего нет. Они могут быть неполными: проверьте, когда они обновлялись."
      }
      action={action}
    />
  );
}

/** Ссылка-кнопка «Назад к источнику» и подобные: открывается в новой вкладке, только http(s). */
export function ExternalLinkButton({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Button variant="secondary" asChild>
      <a href={href} target="_blank" rel="noopener noreferrer">
        <Icon name="external" size={20} />
        {children}
      </a>
    </Button>
  );
}

/** Источник записи для `RecordSheet`: название подключения, адрес страницы, время получения. */
export function sourceFrom(
  provenance: Provenance,
  spaces: ReadonlyMap<string, SpaceInfo>,
  fallbackTitle: string,
): NonNullable<RecordView["source"]> {
  const connection = spaces
    .get(provenance.spaceId)
    ?.connections.find((row) => row.id === provenance.connectionId);
  return {
    title: connection?.title ?? fallbackTitle,
    url: openUrlOf(provenance, connection),
    fetchedAt: Date.parse(provenance.fetchedAt),
    stale: provenance.stale,
    spaceId: provenance.spaceId,
  };
}

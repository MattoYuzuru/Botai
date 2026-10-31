import { Badge, Button, formatDay, formatTime, Sheet, safeHttpUrl } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { sourceChatLinks } from "./chat-links-v4";
import { dataKindsOf, describeSourceKind, describeSourceStatus } from "./status";
import type { StudySource } from "./use-study-sources";

export interface SourceCardV4Props {
  /** Выбранный источник; `null` — шит закрыт. Содержимое остаётся на время анимации закрытия. */
  source: StudySource | null;
  space: SpaceInfo | undefined;
  nowMs: number;
  onClose: () => void;
}

/**
 * Карточка источника (03-sources-and-ux.md §5.3, FR-065): что это, что читает, когда обновлялся, состояние
 * playbook и ошибка, а также действия «Открыть», «Починить», «Спросить агента» и переход в тред задачи.
 * Секретов не показывает: их нет ни в v4-ответе, ни в старом подключении.
 */
export function SourceCardV4({ source, space, nowMs, onClose }: SourceCardV4Props) {
  return (
    <Sheet
      open={source !== null}
      onOpenChange={(open) => !open && onClose()}
      title={source?.title ?? "Источник"}
      description={
        space ? `${space.shortTitle} · ${describeSourceKind(source?.kinds[0] ?? "other").label}` : undefined
      }
      size="half"
    >
      {source && space && <SourceCardDetails source={source} space={space} nowMs={nowMs} onClose={onClose} />}
    </Sheet>
  );
}

function SourceCardDetails({
  source,
  space,
  nowMs,
  onClose,
}: {
  source: StudySource;
  space: SpaceInfo;
  nowMs: number;
  onClose: () => void;
}) {
  const status = describeSourceStatus(source.status);
  const chat = sourceChatLinks(source, space.title);
  const openUrl = safeHttpUrl(source.entryUrl)?.href ?? safeHttpUrl(source.origin)?.href ?? null;
  const lastSync = source.lastSyncAt ? Date.parse(source.lastSyncAt) : null;
  const kinds = dataKindsOf(source.kinds);
  const lastSyncText =
    lastSync === null
      ? "Ещё не обновлялся"
      : `${formatDay(lastSync, nowMs, space.timeZone)}, ${formatTime(lastSync, space.timeZone)}`;

  return (
    <div className="flex flex-col gap-4 pb-2">
      <div className="flex flex-wrap gap-1.5">
        <Badge tone={status.tone}>{status.label}</Badge>
        {source.priority === 0 && <Badge tone="accent">Основной</Badge>}
        {source.trust && source.trust !== "normal" && (
          <Badge tone="neutral">{source.trust === "high" ? "Доверенный" : "Низкое доверие"}</Badge>
        )}
      </div>
      <p className="type-ui text-fg-muted">{status.detail}</p>

      <DetailList label={`Источник: ${source.title}`}>
        <DetailRow icon="source" label="Что это">
          {source.kinds.length > 0
            ? source.kinds.map((kind) => describeSourceKind(kind).label).join(", ")
            : "Вид ещё определяется"}
        </DetailRow>
        <DetailRow icon="document" label="Что читает">
          {kinds.length > 0
            ? kinds.map((kind) => (
                <Badge key={kind} tone="neutral" className="mr-1 mb-1">
                  {describeSourceKind(kind).label}
                </Badge>
              ))
            : "Данных пока нет: агент ещё не прочитал источник"}
        </DetailRow>
        <DetailRow icon="time" label="Последняя синхронизация">
          {lastSyncText}
        </DetailRow>
        <DetailRow icon="repairing" label="Playbook">
          {source.playbookId ? "Настроен: агент знает, как читать источник" : "Ещё не создан"}
        </DetailRow>
        {source.errorDetail && (
          <DetailRow icon="error" label="Что не получается">
            <span className="text-warning-fg">{source.errorDetail}</span>
          </DetailRow>
        )}
        {source.mode === "v4" && source.playbookId && (
          <DetailRow icon="stale" label="История">
            Версии playbook и запуски появятся здесь вместе с API задач.
          </DetailRow>
        )}
      </DetailList>

      <div className="flex flex-col gap-2">
        {openUrl && (
          <Button variant="secondary" leadingIcon="external" asChild fullWidth>
            <a
              href={openUrl}
              target="_blank"
              rel="noopener noreferrer"
              onClick={onClose}
              aria-label={`Открыть сайт: ${source.title}`}
            >
              Открыть
            </a>
          </Button>
        )}
        {chat.repair && (
          <Button variant="secondary" leadingIcon="repairing" asChild fullWidth>
            <Link {...chat.repair} onClick={onClose} aria-label={`Починить: ${source.title}`}>
              Починить
            </Link>
          </Button>
        )}
        {chat.openThread && (
          <Button variant="ghost" leadingIcon="chat" asChild fullWidth>
            <Link {...chat.openThread} onClick={onClose}>
              Открыть тред задачи
            </Link>
          </Button>
        )}
        <Button variant="primary" leadingIcon="chat" asChild fullWidth data-touch-expanded="">
          <Link {...chat.ask} onClick={onClose} aria-label={`Спросить агента: ${source.title}`}>
            Спросить агента
          </Link>
        </Button>
      </div>
      {source.mode === "connections" && (
        <p className="type-caption text-fg-subtle">
          Данные из прежней модели подключений: новый API источников ещё не включён на этом сервере.
        </p>
      )}
    </div>
  );
}

import { Badge, Button, Icon, ListRow } from "@botai/design";
import type { Connection } from "@/api/types";
import {
  connectionKindIcon,
  connectionKindTitle,
  describeConnection,
} from "@/features/data/connection-status";
import { siteUrlOf } from "@/features/data/open-url";
import type { SpaceInfo } from "@/features/data/use-space-directory";

/**
 * Источники одного вуза списком (ЛК, учебная платформа, документация, таблицы, документы): человеческое
 * название, вид, статус и время последнего обновления. Строка открывает управление источником, рядом —
 * «Открыть сайт» (страница входа в данные, а не адрес API). Новый источник добавляется через чат
 * (`AddSourceButton`), а не отдельным мастером.
 */
export function SourceList({
  space,
  nowMs,
  onOpen,
}: {
  space: SpaceInfo;
  nowMs: number;
  onOpen: (connection: Connection) => void;
}) {
  return (
    <ul aria-label={`Источники: ${space.shortTitle}`}>
      {space.connections.map((connection) => {
        const view = describeConnection(connection, nowMs, {
          shortTitle: space.shortTitle,
          timeZone: space.timeZone,
        });
        const site = siteUrlOf(connection);
        return (
          <li
            key={connection.id}
            data-connection-status={connection.status}
            data-connection-kind={connection.kind}
            className="flex items-center gap-1"
          >
            <ListRow
              icon={connectionKindIcon(connection.kind)}
              title={connection.title}
              className="min-w-0 flex-1"
              meta={
                <>
                  <span>{connectionKindTitle(connection.kind)}</span>
                  <Badge tone={view.tone} icon={view.icon}>
                    {view.label}
                  </Badge>
                  <span>{view.detail}</span>
                </>
              }
              onOpen={() => onOpen(connection)}
            />
            {site && (
              <Button variant="ghost" size="md" asChild className="size-11 px-0">
                <a
                  href={site}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Открыть сайт: ${connection.title}`}
                >
                  <Icon name="external" size={20} />
                </a>
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

import { Badge, Banner, Button, SourceStatus, SyncIndicator } from "@botai/design";
import { useIsFetching } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { Connection } from "@/api/types";
import { describeConnection } from "./connection-status";
import { useReconnect } from "./use-reconnect";
import type { SpaceInfo } from "./use-space-directory";

/** Статусы, о которых человеку надо знать сразу. `stale` остаётся подписью свежести: обновление идёт само. */
const NOTICE_STATUSES: ReadonlySet<Connection["status"]> = new Set([
  "needs_reauth",
  "waiting_user",
  "repairing",
  "error",
]);

interface Notice {
  connection: Connection;
  space: SpaceInfo;
  stale: boolean;
}

/**
 * Что показать полосами. С `attentionIds` (ответ `/today.attention`) — именно эти подключения, включая те,
 * чьи данные просто устарели; без них — только подключения, которым нужен человек или починка.
 */
export function collectNotices(spaces: readonly SpaceInfo[], attentionIds?: readonly string[]): Notice[] {
  const wanted = attentionIds ? new Set(attentionIds) : null;
  const notices: Notice[] = [];
  for (const space of spaces) {
    for (const connection of space.connections) {
      if (connection.status === "disabled") continue;
      if (wanted) {
        if (wanted.has(connection.id)) notices.push({ connection, space, stale: true });
      } else if (NOTICE_STATUSES.has(connection.status)) {
        notices.push({ connection, space, stale: false });
      }
    }
  }
  return notices;
}

/**
 * Полосы состояния источников (styleguide §9): «Нужно снова войти», «Вуз что-то поменял, Варя чинит»,
 * «Не удалось обновить». Кнопки повтора нет; действия — только бизнес-смысла: «Войти снова», «Продолжить»,
 * «Спросить в чате».
 */
export function SourceNotices({
  spaces,
  attentionIds,
  nowMs,
}: {
  spaces: readonly SpaceInfo[];
  attentionIds?: readonly string[];
  nowMs: number;
}) {
  const reconnect = useReconnect();
  const notices = collectNotices(spaces, attentionIds);
  if (notices.length === 0) return null;
  const several = spaces.length > 1;
  return (
    <ul aria-label="Состояние источников" className="flex flex-col gap-2">
      {notices.map(({ connection, space, stale }) => {
        const view = describeConnection(connection, nowMs, {
          shortTitle: space.shortTitle,
          timeZone: space.timeZone,
          stale,
        });
        const action =
          view.action === "reauth" ? (
            <Button
              variant="secondary"
              size="sm"
              data-touch-expanded=""
              loading={reconnect.isPending && reconnect.variables === connection.id}
              onClick={() => reconnect.mutate(connection.id)}
            >
              Войти снова
            </Button>
          ) : view.action === "answer" ? (
            <Button variant="secondary" size="sm" data-touch-expanded="" asChild>
              <Link to="/connect">Продолжить</Link>
            </Button>
          ) : view.action === "chat" ? (
            <Button variant="secondary" size="sm" data-touch-expanded="" asChild>
              <Link to="/chat">Спросить в чате</Link>
            </Button>
          ) : undefined;
        return (
          <li key={connection.id}>
            <Banner tone={view.tone} icon={view.icon} action={action}>
              <span
                data-source-state={connection.status}
                data-space-id={space.id}
                className="flex flex-col gap-1"
              >
                <span className="type-label">
                  {several ? `${space.shortTitle} · ${connection.title}` : connection.title}
                </span>
                <span className="type-ui">{view.detail}</span>
              </span>
            </Banner>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Подпись свежести по каждому вузу области экрана: метка вуза и `SourceStatus` («Обновлено 5 минут
 * назад», «Данные от 24 октября», «Нет сети. Показан снимок от …»). У любых данных видны вуз и время.
 */
export function FreshnessFooter({
  spaces,
  nowMs,
  className,
}: {
  spaces: readonly SpaceInfo[];
  nowMs: number;
  className?: string;
}) {
  const rows = spaces.filter((space) => space.health.state !== "none");
  if (rows.length === 0) return null;
  return (
    <ul aria-label="Свежесть данных" className={className ?? "flex flex-col gap-1.5"}>
      {rows.map((space) => (
        <li key={space.id} className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Badge tone="neutral">{space.shortTitle}</Badge>
          <SourceStatus
            state={space.health.state === "none" ? "ok" : space.health.state}
            spaceId={space.id}
            spaceTitle={space.shortTitle}
            updatedAt={space.health.updatedAt}
            nowMs={nowMs}
            timeZone={space.timeZone}
          />
        </li>
      ))}
    </ul>
  );
}

/** Корневые ключи запросов экранов данных: по ним шапка узнаёт, что идёт фоновое обновление. */
const DATA_ROOTS: ReadonlySet<unknown> = new Set([
  "today",
  "schedule",
  "deadlines",
  "courses",
  "grades",
  "people",
  "news",
  "spaces",
]);

/** Маленькая точка в шапке: идёт фоновое обновление (первая загрузка показывается скелетоном, а не точкой). */
export function ScreenSyncIndicator() {
  const fetching = useIsFetching({
    predicate: (query) =>
      query.state.fetchStatus === "fetching" &&
      query.state.dataUpdateCount > 0 &&
      DATA_ROOTS.has(query.queryKey[0]),
  });
  return <SyncIndicator state={fetching > 0 ? "syncing" : "idle"} />;
}

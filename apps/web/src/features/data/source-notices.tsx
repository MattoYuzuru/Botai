import { Banner, Button, type SourceStatusBarItem } from "@botai/design";
import { useIsFetching } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { Connection } from "@/api/types";
import { MascotSyncIndicator } from "@/features/mascot/mascot-sync-indicator";
import { connectionChatLink } from "./chat-links";
import { describeConnection } from "./connection-status";
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
 * Чипы статусов вузов для шапки (`SourceStatusBar`): по одному на вуз с подключениями. Одна функция на
 * «Сегодня», «Расписание», «Задания» и разделы «Учёбы»: подпись — короткое название вуза, а не адрес сайта.
 */
export function sourceStatusItems(spaces: readonly SpaceInfo[]): SourceStatusBarItem[] {
  return spaces.flatMap((space) =>
    space.health.state === "none"
      ? []
      : [
          {
            id: space.id,
            title: space.shortTitle,
            state: space.health.state,
            updatedAt: space.health.updatedAt,
            timeZone: space.timeZone,
          },
        ],
  );
}

const ACTION_LABEL = {
  reauth: "Войти снова",
  link: "Новая ссылка",
  answer: "Продолжить",
  chat: "Спросить в чате",
} as const;

/**
 * Полосы состояния источников над содержимым экрана (styleguide §9): «Нужно снова войти», «Вуз что-то
 * поменял, Варя чинит», «Не удалось обновить». Кнопки повтора нет; действие ведёт в чат: в тред задачи
 * источника (`/chat?thread=`), а если треда нет — в новый чат с готовой просьбой (`/chat?draft=`).
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
        const chat = connectionChatLink(connection, space.title);
        const action = view.action ? (
          <Button variant="secondary" size="sm" data-touch-expanded="" asChild>
            <Link to={chat.to} search={chat.search}>
              {ACTION_LABEL[view.action]}
            </Link>
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
                <span className="type-ui">
                  {connection.errorCode === "login_failed"
                    ? "Вуз отклонил вход. Подождите перед новой попыткой."
                    : view.detail}
                </span>
              </span>
            </Banner>
          </li>
        );
      })}
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
  return <MascotSyncIndicator state={fetching > 0 ? "syncing" : "idle"} />;
}

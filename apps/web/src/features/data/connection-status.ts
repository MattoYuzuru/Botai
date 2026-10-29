import {
  DEFAULT_TIME_ZONE,
  displayHost,
  formatDay,
  formatTime,
  formatUpdated,
  type IconName,
  safeHttpUrl,
} from "@botai/design";
import type { Connection, ConnectionKind, ConnectionStatus } from "@/api/types";

export type ConnectionTone = "success" | "info" | "warning" | "danger" | "neutral";
/** Бизнес-действие рядом со статусом. Кнопки «Повторить» нет: повторы идут в фоне. */
export type ConnectionAction = "reauth" | "answer" | "chat";

export interface ConnectionView {
  tone: ConnectionTone;
  icon: IconName;
  /** Короткая метка: «Работает», «Нужно снова войти». */
  label: string;
  /** Предложение для человека: что с подключением и что будет дальше. */
  detail: string;
  action: ConnectionAction | null;
  /** Требует ли подключение внимания на экране «Сегодня». */
  attention: boolean;
}

const KIND_TITLES: Record<ConnectionKind, string> = {
  portal: "Личный кабинет",
  lms: "Учебная платформа",
  schedule: "Расписание",
  site: "Сайт",
  sheet: "Таблица",
  doc: "Документация",
  document: "Документ",
  calendar: "Календарь",
  other: "Источник",
};

const KIND_ICONS: Record<ConnectionKind, IconName> = {
  portal: "university",
  lms: "course",
  schedule: "schedule",
  site: "site",
  sheet: "table",
  doc: "document",
  document: "document",
  calendar: "today",
  other: "connection",
};

/** Название вида подключения для подписи под именем. */
export const connectionKindTitle = (kind: ConnectionKind): string => KIND_TITLES[kind];

/** Иконка вида подключения: источник узнаётся в списке по виду, а не по адресу. */
export const connectionKindIcon = (kind: ConnectionKind): IconName => KIND_ICONS[kind];

function snapshotText(lastSyncAt: string | null, nowMs: number, timeZone: string): string | null {
  const at = lastSyncAt ? Date.parse(lastSyncAt) : Number.NaN;
  if (!Number.isFinite(at)) return null;
  return `${formatDay(at, nowMs, timeZone)}, ${formatTime(at, timeZone)}`;
}

/**
 * Статус подключения человеческим языком (styleguide §9: без технических кодов, без «токен» и «сессия»).
 * `shortTitle` — «ВШЭ»: подставляется там, где надо назвать вуз.
 */
export function describeConnection(
  connection: Connection,
  nowMs: number,
  options: { shortTitle?: string; timeZone?: string; stale?: boolean } = {},
): ConnectionView {
  const { shortTitle, timeZone = DEFAULT_TIME_ZONE } = options;
  const where = shortTitle ? ` ${shortTitle}` : "";
  const last = connection.lastSyncAt ? Date.parse(connection.lastSyncAt) : Number.NaN;
  const snapshot = snapshotText(connection.lastSyncAt, nowMs, timeZone);
  // Сервер считает данные устаревшими и у «рабочего» подключения, если снимок старше срока свежести адаптера.
  const status: ConnectionStatus =
    options.stale && (connection.status === "active" || connection.status === "syncing")
      ? "stale"
      : connection.status;
  switch (status) {
    case "active":
      return {
        tone: "success",
        icon: "ok",
        label: "Работает",
        detail: Number.isFinite(last)
          ? formatUpdated(last, nowMs, timeZone)
          : "Первое обновление ещё впереди",
        action: null,
        attention: false,
      };
    case "syncing":
      return {
        tone: "info",
        icon: "sync",
        label: "Обновляется",
        detail: snapshot ? `Показаны данные от ${snapshot}` : "Данные скоро появятся",
        action: null,
        attention: false,
      };
    case "learning":
      return {
        tone: "info",
        icon: "sync",
        label: "Настраивается",
        detail: "Агент изучает сайт вуза. Это займёт несколько минут",
        action: null,
        attention: false,
      };
    case "draft":
      return {
        tone: "neutral",
        icon: "pending",
        label: "Не завершено",
        detail: "Подключение начато, но не закончено",
        action: "answer",
        attention: false,
      };
    case "stale":
      return {
        tone: "warning",
        icon: "stale",
        label: "Данные устарели",
        detail: snapshot ? `Показаны данные от ${snapshot}. Обновление идёт само` : "Обновление идёт само",
        action: null,
        attention: true,
      };
    case "needs_reauth":
      return {
        tone: "warning",
        icon: "reauth",
        label: "Нужно снова войти",
        detail: `Нужно снова войти в личный кабинет${where}. Старые данные помечены устаревшими`,
        action: "reauth",
        attention: true,
      };
    case "waiting_user":
      return {
        tone: "info",
        icon: "waiting",
        label: "Ждёт вашего ответа",
        detail: `BotAI ждёт вашего ответа, чтобы продолжить подключение${where}`,
        action: "answer",
        attention: true,
      };
    case "repairing":
      return {
        tone: "info",
        icon: "repairing",
        label: "Чинится",
        detail: "Вуз что-то поменял, Варя чинит",
        action: null,
        attention: true,
      };
    case "error":
      return {
        tone: "danger",
        icon: "error",
        label: "Не обновляется",
        detail:
          connection.errorDetail ||
          `Не удалось обновить данные${where}. ${snapshot ? `Показаны данные от ${snapshot}` : "Спросите в чате, что случилось"}`,
        action: "chat",
        attention: true,
      };
    case "disabled":
      return {
        tone: "neutral",
        icon: "paused",
        label: "Отключено",
        detail: "Данные этого источника не показываются",
        action: null,
        attention: false,
      };
  }
}

/** Адрес источника без схемы: «lk.fakeuni.test». Источник без адреса (документ) — только вид. */
export function originLabel(connection: Connection): string {
  const url = safeHttpUrl(connection.origin);
  return url ? displayHost(url) : connectionKindTitle(connection.kind);
}

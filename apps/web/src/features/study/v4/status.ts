import type { IconName } from "@botai/design";
import type { SourceKindV4, SourceNatureKindV4, SourceStatusV4 } from "@/api/sources";

/**
 * Словарь источника для интерфейса (03-sources-and-ux.md §5.2, §6.2): статус — цвет + текст + иконка,
 * чтобы цвет не был единственным сигналом; вид — иконка дизайн-системы и название словами.
 */
export type SourceTone = "success" | "info" | "warning" | "danger" | "neutral";

export interface SourceStatusView {
  label: string;
  /** Что происходит — для карточки источника и `aria-label` строки. */
  detail: string;
  tone: SourceTone;
  icon: IconName;
  /** Требует действия человека прямо сейчас (вход, ссылка, ответ). */
  needsUser: boolean;
  /** Источник сам по себе не обновляется, пока его не включат. */
  disabled: boolean;
}

const STATUS_VIEW: Record<SourceStatusV4, SourceStatusView> = {
  draft: {
    label: "Настраивается",
    detail: "Источник создан: агент ещё ничего не прочитал.",
    tone: "neutral",
    icon: "waiting",
    needsUser: false,
    disabled: false,
  },
  probing: {
    label: "Проверяет доступ",
    detail: "Агент пробует источник и ищет в нём данные.",
    tone: "info",
    icon: "search",
    needsUser: false,
    disabled: false,
  },
  learning: {
    label: "Учится читать",
    detail: "Агент собирает и проверяет playbook источника.",
    tone: "info",
    icon: "repairing",
    needsUser: false,
    disabled: false,
  },
  waiting_user: {
    label: "Ждёт вас",
    detail: "Нужен вход, код, ссылка или выбор — вопрос в чате источника.",
    tone: "warning",
    icon: "waiting",
    needsUser: true,
    disabled: false,
  },
  active: {
    label: "Работает",
    detail: "Данные свежие, обновление идёт по расписанию.",
    tone: "success",
    icon: "ok",
    needsUser: false,
    disabled: false,
  },
  syncing: {
    label: "Обновляется",
    detail: "Идёт плановое обновление данных.",
    tone: "info",
    icon: "sync",
    needsUser: false,
    disabled: false,
  },
  stale: {
    label: "Устарел",
    detail: "Последнее успешное чтение было давно; данные показываются с пометкой.",
    tone: "warning",
    icon: "stale",
    needsUser: false,
    disabled: false,
  },
  needs_reauth: {
    label: "Нужен вход",
    detail: "Сессия источника протухла: данные остаются, нужен повторный вход.",
    tone: "warning",
    icon: "reauth",
    needsUser: true,
    disabled: false,
  },
  needs_link: {
    label: "Нужна ссылка",
    detail: "Личная ссылка (календарь, почта) перестала работать.",
    tone: "warning",
    icon: "source",
    needsUser: true,
    disabled: false,
  },
  error: {
    label: "Ошибка",
    detail: "Источник изменился или сломался: агент может починить playbook.",
    tone: "danger",
    icon: "error",
    needsUser: false,
    disabled: false,
  },
  disabled: {
    label: "Выключен",
    detail: "Источник выключен владельцем: задач нет, данные скрыты.",
    tone: "neutral",
    icon: "paused",
    needsUser: false,
    disabled: true,
  },
  archived: {
    label: "В архиве",
    detail: "Источник убран из работы; данные сохранены.",
    tone: "neutral",
    icon: "paused",
    needsUser: false,
    disabled: true,
  },
};

/** Статус источника human-языком; неизвестное значение с бэкенда не ломает экран. */
export function describeSourceStatus(status: SourceStatusV4 | string): SourceStatusView {
  return Object.hasOwn(STATUS_VIEW, status)
    ? STATUS_VIEW[status as SourceStatusV4]
    : {
        label: "Неизвестно",
        detail: "Источник в состоянии, которого интерфейс ещё не знает.",
        tone: "neutral",
        icon: "connection",
        needsUser: false,
        disabled: false,
      };
}

interface KindView {
  label: string;
  icon: IconName;
}

const KIND_VIEW: Record<SourceKindV4, KindView> = {
  // Природа: что это за точка доступа.
  site: { label: "Сайт", icon: "site" },
  portal: { label: "Личный кабинет", icon: "university" },
  lms: { label: "Учебная платформа", icon: "course" },
  api: { label: "API", icon: "connection" },
  calendar: { label: "Календарь", icon: "today" },
  sheet: { label: "Таблица", icon: "table" },
  doc: { label: "Документация", icon: "document" },
  file: { label: "Файл", icon: "file" },
  mail: { label: "Почта", icon: "chat" },
  feed: { label: "Лента", icon: "news" },
  other: { label: "Источник", icon: "connection" },
  // Виды данных: что источник отдаёт.
  schedule: { label: "Расписание", icon: "schedule" },
  deadlines: { label: "Дедлайны", icon: "deadline" },
  assignments: { label: "Задания", icon: "deadline" },
  courses: { label: "Предметы", icon: "course" },
  grades: { label: "Оценки", icon: "grade" },
  people: { label: "Преподаватели", icon: "person" },
  locations: { label: "Аудитории", icon: "room" },
  news: { label: "Новости", icon: "news" },
  documents: { label: "Документы", icon: "document" },
  messages: { label: "Сообщения", icon: "chat" },
};

const FALLBACK_KIND: KindView = { label: "Источник", icon: "connection" };

/** Иконка и подпись вида; неизвестный вид с бэкенда показывается нейтрально. */
export function describeSourceKind(kind: SourceKindV4 | string): KindView {
  return Object.hasOwn(KIND_VIEW, kind) ? KIND_VIEW[kind as SourceKindV4] : FALLBACK_KIND;
}

/** Первая «природа» источника — иконка строки; иначе первый вид. */
export function sourceIcon(kinds: readonly string[]): IconName {
  const nature = kinds.find((kind) => Object.hasOwn(NATURE_KINDS, kind));
  return describeSourceKind(nature ?? kinds[0] ?? "other").icon;
}

const NATURE_KINDS: Record<SourceNatureKindV4, true> = {
  site: true,
  portal: true,
  lms: true,
  api: true,
  calendar: true,
  sheet: true,
  doc: true,
  file: true,
  mail: true,
  feed: true,
  other: true,
};

/** Виды данных источника — без природы: их показывают чипами «что читает». */
export function dataKindsOf(kinds: readonly string[]): string[] {
  return kinds.filter((kind) => !Object.hasOwn(NATURE_KINDS, kind));
}

/** Природа источника — без видов данных: их показывают чипами «что это». */
export function natureKindsOf(kinds: readonly string[]): string[] {
  return kinds.filter((kind) => Object.hasOwn(NATURE_KINDS, kind));
}

/** Название источника словами для пустых подписей и чата: виды данных, иначе природа. */
export function kindsLabel(kinds: readonly string[]): string {
  const shown = dataKindsOf(kinds);
  return (shown.length > 0 ? shown : kinds).map((kind) => describeSourceKind(kind).label).join(", ");
}

export interface SourcesSummary {
  tone: SourceTone;
  icon: IconName;
  label: string;
}

/**
 * Сводный статус вуза (03-sources-and-ux.md §5.2): «всё обновляется» / «источник ждёт вашего действия» /
 * «есть ошибка». Отключённые и архивные источники не считаются, как и на старых экранах.
 */
export function summarizeSources(statuses: readonly (SourceStatusV4 | string)[]): SourcesSummary | null {
  const live = statuses.filter((status) => status !== "disabled" && status !== "archived");
  if (live.length === 0) return null;
  if (
    live.some((status) => status === "needs_reauth" || status === "needs_link" || status === "waiting_user")
  ) {
    return { tone: "warning", icon: "reauth", label: "Источник ждёт вашего действия" };
  }
  if (live.some((status) => status === "error")) {
    return { tone: "danger", icon: "error", label: "Есть ошибка источника" };
  }
  if (live.some((status) => status === "stale")) {
    return { tone: "warning", icon: "stale", label: "Данные одного источника устарели" };
  }
  if (live.some((status) => status === "draft" || status === "probing" || status === "learning")) {
    return { tone: "info", icon: "sync", label: "Источник настраивается" };
  }
  if (live.some((status) => status === "syncing")) {
    return { tone: "info", icon: "sync", label: "Обновляется" };
  }
  return { tone: "success", icon: "ok", label: "Всё обновляется" };
}

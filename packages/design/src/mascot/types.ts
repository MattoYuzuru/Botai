import type { LineFields, LineKey } from "./lines";

/**
 * Типы маскота Варя (docs/design/mascot.md §2). Состояние вычисляет чистая функция `resolveMascot`;
 * renderer получает только готовый объект `MascotPresentation`, никаких учебных данных, профиля,
 * ответов сервера и истории чата (правило design-lint `mascot-data`).
 */

/** Ситуации renderer (`Varya.SITUATIONS`): семь из doodle_vibe и девять добавленных Botai. */
export const MASCOT_SITUATIONS = [
  "connect",
  "tip",
  "deadline",
  "skips",
  "night",
  "class",
  "done",
  "sync",
  "think",
  "overdue",
  "offline",
  "reauth",
  "error",
  "idle",
  "perch",
  "wave",
] as const;
export type MascotSituationId = (typeof MASCOT_SITUATIONS)[number];

export type MascotTheme = "day" | "orig" | "pj";
export type MascotMotion = "animated" | "static";

/** Сезонные образы renderer (`Varya.SKINS`). */
export const MASCOT_SKINS = ["new-year", "mug", "leaf"] as const;
export type MascotSkinId = (typeof MASCOT_SKINS)[number];

/** Одноразовые действия сцены (`Varya.ACTS`). */
export const MASCOT_ACTS = ["peck", "shake", "look", "wave", "nod", "hop"] as const;
export type MascotAct = (typeof MASCOT_ACTS)[number];

export type MascotLoop = "run" | "calm" | "tilt";

export type MascotScreen = "chat" | "schedule" | "deadlines" | "progress" | "other";
export type MascotAvailability = "ready" | "unknown" | "empty" | "offline" | "reauth" | "repairing";
export type MascotActivity = "idle" | "connecting" | "syncing" | "thinking";

/**
 * Что Варя просит сделать. Нажатие отдаёт наверх такой объект, разрешает его frontend: renderer
 * ничего не исполняет. «Повторить» среди намерений нет — повторов в интерфейсе нет (styleguide §9).
 */
export type MascotIntentKind =
  | "open-deadline"
  | "open-chat"
  | "open-source"
  | "open-progress"
  | "open-schedule"
  | "reauth"
  | "connect";

export interface MascotIntent {
  kind: MascotIntentKind;
  /** Подпись кнопки на `MascotStage` («Открыть срочную»). */
  label: string;
  /** Для `open-deadline`: какая работа выбрана. */
  deadlineId?: string;
}

export interface MascotDeadline {
  id: string;
  title: string;
  /** Срок в мс; `null` или нечисловое значение — «срок не указан». */
  dueMs: number | null;
  done: boolean;
}

export interface MascotSnapshot {
  spaceId: string;
  /** Когда снимок получен, мс. */
  updatedAt: number;
  /** Снимок устарел: последняя синхронизация не удалась или давно не было новой. */
  stale: boolean;
  nextClass: { startMs: number; title: string; room?: string } | null;
  /**
   * Сколько пар сегодня и когда первая. Нужно только для приветствия пустого чата; без поля
   * реплика обходится вариантом без числа.
   */
  today?: { classCount: number; firstStartMs: number | null } | null;
  deadlines: readonly MascotDeadline[];
}

export interface MascotRecentEvent {
  id: string;
  spaceId: string;
  kind: "deadline-done" | "space-connected";
  at: number;
  /** Название сданной работы для реплики «{title} сдано». */
  title?: string;
}

export interface MascotContext {
  /** Активное пространство вуза. Снимки и события других пространств отбрасываются. */
  spaceId: string;
  /** Короткое название вуза для реплик («ВШЭ»). */
  spaceTitle?: string | null;
  /** Те же часы, что ушли в UI. Скрытого `Date.now()` в `resolveMascot` нет. */
  nowMs: number;
  /** Часовой пояс пространства. */
  timeZone: string;
  screen: MascotScreen;
  availability: MascotAvailability;
  activity: MascotActivity;
  /** Идущая работа агента: когда началась и какая стадия (из `event: status`), для реплик «Обновляю …», «Читаю …». */
  work?: { startedAtMs: number | null; stage: string | null } | null;
  snapshot: MascotSnapshot | null;
  recentEvent: MascotRecentEvent | null;
  acknowledgedEventIds: readonly string[];
  preferences: { seasonal: boolean; motion: "system" | "calm" };
  reducedMotion: boolean;
  /** Слабая сеть или устройство, экономия. */
  lite: boolean;
  /** `document.visibilityState === 'visible'`. */
  visible: boolean;
  keyboardOpen: boolean;
}

export interface MascotBadge {
  tone: "warning" | "danger" | "neutral";
  text: string;
}

export interface MascotPresentation {
  situation: MascotSituationId;
  theme: MascotTheme;
  motion: MascotMotion;
  /** Реплика из реестра, числа уже подставлены из снимка. `null` — Варя молчит. */
  line: string | null;
  badge: MascotBadge | null;
  intent: MascotIntent | null;
  /** Сезонный образ (§7) или `null`. */
  skin: MascotSkinId | null;
  /** Показывать ли саму картинку. Текст (реплика, бейдж, действие) остаётся при любом значении. */
  visible: boolean;
  /**
   * Не `null`, когда сейчас идёт разовая реакция на событие (сдано, подключено): после неё интерфейс
   * вызывает `acknowledge(id)`, и на возврате вкладки или после перезагрузки она не повторяется.
   */
  reactionEventId: string | null;
  /** Какое окно календаря дало образ (для витрины и тестов). */
  seasonalEventId: string | null;
  /**
   * Из какой записи реестра реплик и с какими полями собрана `line`: по нажатию на Варю берётся следующая реплика
   * той же ситуации (`cycleLines`), без нового запроса данных.
   */
  lineKey: LineKey | null;
  lineFields: LineFields;
}

/**
 * Детерминированные фикстуры Вари (docs/design/mascot.md §2 и §8). Синтетические данные: вход в вуз не нужен,
 * время зафиксировано, поэтому один и тот же вход всегда даёт одну и ту же Варю. Это не доказательство живого
 * входа в вуз, настоящих данных или измеренной производительности.
 */
import type { MascotContext, MascotDeadline, MascotSnapshot } from "@botai/design";

export const MSK = "Europe/Moscow";
export const HOUR = 3_600_000;
export const MINUTE = 60_000;
/** Вторник, 27 октября 2026, 14:05 по Москве. */
export const NOW = Date.UTC(2026, 9, 27, 11, 5);
/** Момент по Москве (UTC+3, без перехода на летнее время). */
export const msk = (month: number, day: number, hour: number, minute = 0, year = 2026) =>
  Date.UTC(year, month - 1, day, hour - 3, minute);

export const deadline = (id: string, title: string, dueMs: number | null, done = false): MascotDeadline => ({
  id,
  title,
  dueMs,
  done,
});

export function snapshot(
  deadlines: readonly MascotDeadline[],
  extra: Partial<MascotSnapshot> = {},
): MascotSnapshot {
  return {
    spaceId: "hse",
    updatedAt: NOW - 5 * MINUTE,
    stale: false,
    nextClass: null,
    today: { classCount: 3, firstStartMs: msk(10, 27, 9, 30) },
    deadlines,
    ...extra,
  };
}

export const URGENT = snapshot([deadline("hw3", "Домашнее задание 3", NOW + 5 * HOUR)]);
export const OVERDUE = snapshot([deadline("essay", "Эссе по истории", NOW - 3 * HOUR)]);
export const UNKNOWN_DUE = snapshot([deadline("reading", "Чтение: глава 4", null)]);
export const ALL_DONE = snapshot([
  deadline("a", "Лабораторная работа 2", NOW - 20 * HOUR, true),
  deadline("b", "Проект", NOW + 80 * HOUR, true),
]);
export const SOON = snapshot([deadline("lab", "Лабораторная работа 2", NOW + 50 * HOUR)]);
export const CLASS_SOON = snapshot([], {
  nextClass: { startMs: NOW + 40 * MINUTE, title: "Базы данных", room: "ауд. 305" },
});

export const BASE: MascotContext = {
  spaceId: "hse",
  spaceTitle: "ВШЭ",
  nowMs: NOW,
  timeZone: MSK,
  screen: "chat",
  availability: "ready",
  activity: "idle",
  work: null,
  snapshot: snapshot([]),
  recentEvent: null,
  acknowledgedEventIds: [],
  preferences: { seasonal: true, motion: "system" },
  reducedMotion: false,
  lite: false,
  visible: true,
  keyboardOpen: false,
};

export interface MascotFixture {
  id: string;
  title: string;
  /** Что должно получиться: пункт таблицы §2 или §8. */
  expect: string;
  context: Partial<MascotContext>;
}

/** Таблица приоритета §2: по фикстуре на каждую строку. */
export const PRIORITY_FIXTURES: readonly MascotFixture[] = [
  {
    id: "p1-reauth",
    title: "1. Нужен повторный вход",
    expect: "reauth, «Войти снова», сдержанное движение",
    context: { availability: "reauth" },
  },
  {
    id: "p2-offline",
    title: "2. Нет сети",
    expect: "offline, статично, действия нет — повтор сам при сети",
    context: { availability: "offline" },
  },
  {
    id: "p3-repairing",
    title: "3. Вуз что-то поменял",
    expect: "sync, бег на месте, «Что случилось» ведёт в чат",
    context: { availability: "repairing" },
  },
  {
    id: "p4-connecting",
    title: "4. Подключаем вуз",
    expect: "connect, бег на месте, «Открыть ход»",
    context: { activity: "connecting", work: { startedAtMs: NOW - 8_000, stage: "список курсов" } },
  },
  {
    id: "p5-syncing",
    title: "5. Обновляем",
    expect: "sync, «Обновляю ВШЭ, идёт …», «Открыть ход»",
    context: { activity: "syncing", work: { startedAtMs: NOW - 72_000, stage: null } },
  },
  {
    id: "p6-thinking",
    title: "6. Агент думает",
    expect: "think, «Ищу в ВШЭ» без обещания результата",
    context: { activity: "thinking" },
  },
  {
    id: "p7-event",
    title: "7. Работа только что сдана",
    expect: "done, одна реакция ≤ 1.5 с",
    context: {
      snapshot: URGENT,
      recentEvent: {
        id: "hw2",
        spaceId: "hse",
        kind: "deadline-done",
        at: NOW - 5_000,
        title: "Домашнее задание 2",
      },
    },
  },
  {
    id: "p8-overdue",
    title: "8. Срок прошёл",
    expect: "overdue, «Просрочено», статичный знак, без упрёков",
    context: { snapshot: OVERDUE },
  },
  {
    id: "p9-deadline",
    title: "9. До срока ≤ 24 ч",
    expect: "deadline, «Осталось 5 ч», «Открыть срочную»",
    context: { snapshot: URGENT },
  },
  {
    id: "p10-tip",
    title: "10. До срока 24–72 ч",
    expect: "tip, «Скоро · 2 д», «Открыть работу»",
    context: { snapshot: SOON },
  },
  {
    id: "p11-class",
    title: "11. Пара через 40 минут",
    expect: "class, будильник, «Открыть расписание»",
    context: { snapshot: CLASS_SOON },
  },
  {
    id: "p12-empty",
    title: "12. Вуз не подключён",
    expect: "connect, статично, «Подключить вуз»",
    context: { availability: "empty", snapshot: null },
  },
  {
    id: "p13-night",
    title: "13. Ночь по времени пространства",
    expect: "night, ночной колпак (тема pj), «Уже поздно» или число работ",
    context: { nowMs: msk(10, 27, 23, 30), snapshot: snapshot([deadline("x", "Эссе", msk(11, 10, 12))]) },
  },
  {
    id: "p14-all-done",
    title: "14. Всё сдано, снимок свежий",
    expect: "done без реакции, статично, «Пока всё сдано»",
    context: { snapshot: ALL_DONE },
  },
  {
    id: "p15-idle",
    title: "15. Иначе",
    expect: "idle, приветствие из снимка",
    context: {},
  },
];

/** Фикстуры проверок §8, которые не требуют взаимодействия. */
export const CHECK_FIXTURES: readonly MascotFixture[] = [
  {
    id: "urgent",
    title: "urgent — срок через 5 ч",
    expect: "Поза deadline, бейдж «Осталось 5 ч», действие «Открыть срочную»",
    context: { snapshot: URGENT },
  },
  {
    id: "urgent-reduced",
    title: "urgent + reducedMotion",
    expect: "То же статично, та же информация и действие",
    context: { snapshot: URGENT, reducedMotion: true },
  },
  {
    id: "urgent-reauth",
    title: "urgent + reauth",
    expect: "Первична просьба войти снова; старый срок помечен устаревшим; сезонного слоя нет",
    context: { snapshot: URGENT, availability: "reauth", nowMs: msk(12, 25, 12) },
  },
  {
    id: "overdue",
    title: "overdue",
    expect: "«Просрочено», без упрёков, бесконечной тревоги и реакции",
    context: { snapshot: OVERDUE },
  },
  {
    id: "due-unknown",
    title: "due-unknown",
    expect: "Поза tip, «Срок не указан», обратного отсчёта нет",
    context: { snapshot: UNKNOWN_DUE },
  },
  {
    id: "no-data",
    title: "no-data",
    expect: "connect + «Подключить вуз»; не выглядит как «всё сдано»",
    context: { availability: "empty", snapshot: null },
  },
  {
    id: "stale-after-failed-sync",
    title: "stale-after-failed-sync",
    expect: "Старые данные помечены устаревшими; «всё сдано» не показывается",
    context: { snapshot: { ...ALL_DONE, stale: true } },
  },
  {
    id: "stale-urgent",
    title: "stale + срочная работа",
    expect: "Срок остаётся виден и помечен «устарело»",
    context: { snapshot: { ...URGENT, stale: true } },
  },
  {
    id: "thinking",
    title: "thinking",
    expect: "think, «Ищу в …», без обещания результата",
    context: { activity: "thinking" },
  },
  {
    id: "new-year-urgent",
    title: "new-year + urgent",
    expect: "Колпак и срок одновременно; читаемость срока не пострадала",
    context: {
      snapshot: snapshot([deadline("hw3", "Домашнее задание 3", msk(12, 25, 17))]),
      nowMs: msk(12, 25, 12),
    },
  },
  {
    id: "seasonal-off",
    title: "seasonal: false",
    expect: "Базовая Варя, срочность, реплика и действие сохранены",
    context: {
      snapshot: snapshot([deadline("hw3", "Домашнее задание 3", msk(12, 25, 17))]),
      nowMs: msk(12, 25, 12),
      preferences: { seasonal: false, motion: "system" },
    },
  },
  {
    id: "calm",
    title: "calm — «спокойный маскот»",
    expect: "Статичная поза, циклы остановлены, смысл сохранён",
    context: { snapshot: URGENT, preferences: { seasonal: true, motion: "calm" } },
  },
  {
    id: "lite",
    title: "lite — слабая сеть или устройство",
    expect: "Статичная поза, смысл сохранён",
    context: { snapshot: URGENT, lite: true },
  },
  {
    id: "hidden",
    title: "visible: false — скрытая вкладка",
    expect: "Картинки нет, бейдж, реплика и действие на месте",
    context: { snapshot: URGENT, visible: false },
  },
];

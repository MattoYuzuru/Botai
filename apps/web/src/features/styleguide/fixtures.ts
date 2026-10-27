/**
 * Синтетические данные витрины /styleguide. Вход в вуз не нужен, время зафиксировано —
 * одинаковый вход всегда даёт одинаковый экран (NFR-016). Это не доказательство живых данных.
 */
import type { ScheduleItem, SwitcherSpace, TabItem, ThinkingStep, WeekDay } from "@botai/design";

export const MSK = "Europe/Moscow";
/** Вторник, 27 октября 2026, 14:05 по Москве. */
export const NOW = Date.UTC(2026, 9, 27, 11, 5);
const HOUR = 3_600_000;
const at = (day: number, hour: number, minute = 0) => Date.UTC(2026, 9, day, hour - 3, minute);

export const MAIN_TABS: TabItem[] = [
  { id: "today", label: "Сегодня", icon: "today", href: "/" },
  { id: "schedule", label: "Расписание", icon: "schedule", href: "/schedule" },
  { id: "tasks", label: "Задания", icon: "deadline", href: "/tasks" },
  { id: "chat", label: "Чат", icon: "chat", href: "/chat", badge: 1 },
  { id: "study", label: "Учёба", icon: "university", href: "/study" },
];

export const SPACES: SwitcherSpace[] = [
  {
    id: "hse",
    title: "Высшая школа экономики",
    shortTitle: "ВШЭ",
    hue: 262.2,
    status: "ok",
    updatedAt: NOW - 5 * 60_000,
  },
  {
    id: "cu",
    title: "Центральный университет",
    shortTitle: "ЦУ",
    hue: 292,
    status: "reauth",
    updatedAt: at(24, 9),
  },
  {
    id: "reserve",
    title: "Резервный вуз",
    shortTitle: "Резерв",
    hue: 325,
    status: "stale",
    updatedAt: at(24, 9),
  },
];

export const SCHEDULE: ScheduleItem[] = [
  {
    id: "s1",
    start: at(27, 9, 30),
    end: at(27, 10, 50),
    title: "Алгоритмы и структуры данных",
    room: "Ауд. 214",
    teacher: "Иванов А. П.",
    spaceId: "hse",
    spaceTitle: "ВШЭ",
  },
  {
    id: "s2",
    start: at(27, 13, 0),
    end: at(27, 14, 20),
    title: "Базы данных",
    room: "Ауд. 305",
    spaceId: "cu",
    spaceTitle: "ЦУ",
    changed: true,
    conflict: true,
  },
  {
    id: "s3",
    start: at(27, 14, 0),
    end: at(27, 15, 30),
    title: "Английский язык",
    room: "Ауд. 118",
    spaceId: "hse",
    spaceTitle: "ВШЭ",
    conflict: true,
  },
  {
    id: "s4",
    start: at(27, 18, 0),
    end: at(27, 19, 20),
    title: "Теория вероятностей",
    spaceId: "cu",
    spaceTitle: "ЦУ",
  },
];

export const WEEK: WeekDay[] = Array.from({ length: 7 }, (_, index) => ({
  date: at(26 + index, 12),
  count: [0, 4, 3, 2, 0, 1, 0][index] ?? 0,
  hasConflict: index === 1,
}));

export const DEADLINES = [
  {
    id: "d1",
    title: "Домашнее задание 3",
    course: "Алгоритмы",
    dueAt: NOW + 5 * HOUR,
    spaceId: "hse",
    spaceTitle: "ВШЭ",
  },
  {
    id: "d2",
    title: "Лабораторная работа 2",
    course: "Базы данных",
    dueAt: NOW + 50 * HOUR,
    spaceId: "cu",
    spaceTitle: "ЦУ",
  },
  {
    id: "d3",
    title: "Эссе по истории",
    course: "История",
    dueAt: NOW - 2 * HOUR,
    spaceId: "hse",
    spaceTitle: "ВШЭ",
  },
  {
    id: "d4",
    title: "Проект: итоговая презентация",
    course: "Менеджмент",
    dueAt: NOW + 12 * 24 * HOUR,
    spaceId: "hse",
    spaceTitle: "ВШЭ",
  },
  { id: "d5", title: "Чтение: глава 4", course: "Экономика", dueAt: null, spaceId: "cu", spaceTitle: "ЦУ" },
] as const;

export const THINKING: ThinkingStep[] = [
  { id: "t1", label: "Ищу личный кабинет", state: "done", at: NOW - 24_000 },
  { id: "t2", label: "Читаю список курсов", state: "done", at: NOW - 17_000 },
  {
    id: "t3",
    label: "Открываю расписание",
    state: "failed",
    detail: "Страница не ответила, попробую ещё раз сам",
    at: NOW - 11_000,
  },
  { id: "t4", label: "Читаю расписание", state: "running", at: NOW - 6_000 },
];

export const PROPOSAL = {
  id: "01JAAAAAAAAAAAAAAAAAAAAAB1",
  intent: "Подать заявку на справку об обучении",
  method: "POST" as const,
  host: "fakeuni.test",
  reversible: false,
  expiresAt: NOW + 10 * 60_000,
  spaceTitle: "ВШЭ",
  fields: [
    { name: "type", label: "Тип справки", value: "Об обучении", editable: false },
    { name: "copies", label: "Количество копий", value: "1", editable: true },
  ],
};

export const SAMPLE_MARKDOWN = [
  "Следующая пара — **Алгоритмы и структуры данных** в 9:30, ауд. 214.",
  "",
  "- Сегодня три пары, одна из них изменилась: `Базы данных` перенесены на 13:00.",
  "- Домашнее задание 3 нужно сдать сегодня до 19:05.",
  "",
  "Подробности — в [личном кабинете](https://lk.fakeuni.test/timetable).",
].join("\n");

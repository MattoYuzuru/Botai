/**
 * Учебные данные мок-режима (расписание, сроки, предметы, оценки, люди, новости, документы). Вуз вымышленный
 * (`fakeuni.test`); данные строятся от «сегодня» мока, поэтому экран всегда выглядит одинаково: сегодня есть
 * пары, одна из них пересекается с парой другого вуза, а в сроках есть горящие, просроченные и без срока.
 * Форма строк — по сгенерированным типам OpenAPI, расширенным контрактом v3 (`api/v3-data.ts`): у пар есть
 * адрес, группа, тип словами источника, номер пары, онлайн-ссылки и атрибуты; адрес API (`sourceUrl`) отличается
 * от страницы интерфейса (`openUrl`), и экраны обязаны показывать только вторую.
 */
import type { components } from "@/api/schema.gen";
import type { Deadline, ScheduleEvent } from "@/api/types";
import type {
  Course,
  EventAttribute,
  EventLink,
  Grade,
  GradeLayer,
  NewsItem,
  Person,
  Provenance,
  StudyUpdate,
} from "@/api/v3-data";
import { addDays, startOfDay } from "@/features/data/dates";
import { weekOf } from "@/features/schedule/week";
import type { MockConnection, MockDb } from "./db";

type S = components["schemas"];
type Connection = MockConnection;

export const MOCK_ZONE = "Europe/Moscow";
const HOUR = 3_600_000;

/** Какой вуз сидит в пространстве: по справочнику (`universityId`). Свои пространства данных не получают. */
export type SeedSource = "hse" | "cu";

export function seedSourceOf(db: MockDb, spaceId: string): SeedSource | null {
  const universityId = db.spaces.find((space) => space.id === spaceId)?.universityId;
  return universityId === "u-hse" ? "hse" : universityId === "u-cu" ? "cu" : null;
}

/** Подключение вуза, из которого «пришли» данные: расписание — календарь, остальное — кабинет или LMS. */
function connectionFor(
  db: MockDb,
  source: SeedSource,
  topic: "schedule" | "portal",
): { connection: Connection; spaceId: string } | null {
  const space = db.spaces.find((row) => seedSourceOf(db, row.id) === source);
  if (!space) return null;
  const mine = db.connections.filter((c) => c.spaceId === space.id && c.status !== "disabled");
  const wanted =
    source === "hse"
      ? mine.find((c) => c.kind === (topic === "schedule" ? "schedule" : "portal"))
      : mine.find((c) => c.kind === "lms");
  const connection = wanted ?? mine[0];
  return connection ? { connection, spaceId: space.id } : null;
}

const NEEDS_ATTENTION = new Set<Connection["status"]>(["stale", "needs_reauth", "repairing"]);

function provenance(
  db: MockDb,
  source: SeedSource,
  topic: "schedule" | "portal",
  path: string,
  query = "",
): Provenance | null {
  const found = connectionFor(db, source, topic);
  if (!found) return null;
  const { connection, spaceId } = found;
  const fetched = connection.lastSyncAt ?? new Date(db.now() - 5 * 60_000).toISOString();
  return {
    connectionId: connection.id,
    spaceId,
    // Служебный адрес API: на экране его нет.
    sourceUrl: `${connection.origin}/api${path}${query}`,
    // Страница интерфейса, где запись видна человеку.
    openUrl: `${connection.origin}${path}${query}`,
    fetchedAt: fetched,
    stale: NEEDS_ATTENTION.has(connection.status) || db.now() - Date.parse(fetched) > 24 * HOUR,
  };
}

// --- расписание ----------------------------------------------------------------------------------

interface EventTemplate {
  /** День цикла из семи: 0 — «сегодня», 5 и 6 — пустые дни. */
  day: number;
  at: readonly [number, number];
  minutes: number;
  title: string;
  kind: S["ScheduleEventOut"]["kind"];
  format: S["ScheduleEventOut"]["format"];
  room?: string;
  teacher?: string;
  course?: string;
  source: SeedSource;
  changed?: boolean;
  /** Тип занятия словами источника — то, что вуз пишет на бейдже. */
  kindText?: string;
  address?: string;
  group?: string;
  lesson?: number;
  /** Ссылка занятия от источника (поле `url`): отдельно от `links[]`. */
  url?: string;
  links?: readonly EventLink[];
  attributes?: readonly EventAttribute[];
}

/** Здание синтетического вуза: адрес и корпус одни и те же во всех парах, как на сайте настоящего вуза. */
const HSE_ADDRESS = "ул. Учебная, д. 11";
const HSE_GROUP = "ПМ-3 курс 26/27";
const HSE_CAMPUS: EventAttribute = { label: "Корпус", value: "Главный корпус" };

const EVENT_TEMPLATES: readonly EventTemplate[] = [
  {
    day: 0,
    at: [9, 30],
    minutes: 80,
    title: "Математический анализ",
    kind: "lecture",
    format: "offline",
    room: "ауд. 305",
    teacher: "Петров А. В.",
    course: "Математический анализ",
    source: "hse",
    address: HSE_ADDRESS,
    group: HSE_GROUP,
    lesson: 2,
    attributes: [HSE_CAMPUS],
  },
  {
    day: 0,
    at: [11, 10],
    minutes: 80,
    title: "Алгоритмы и структуры данных",
    kind: "seminar",
    format: "offline",
    room: "ауд. 412",
    teacher: "Сидорова Е. Н.",
    course: "Алгоритмы и структуры данных",
    source: "hse",
    changed: true,
    kindText: "Текущий рубежный контроль",
    address: HSE_ADDRESS,
    group: HSE_GROUP,
    lesson: 3,
    links: [{ url: "https://lk.fakeuni.test/materials/algorithms", label: "Материалы занятия" }],
    attributes: [HSE_CAMPUS, { label: "Форма контроля", value: "Контрольная работа" }],
  },
  {
    day: 0,
    at: [11, 30],
    minutes: 90,
    title: "Основы продуктового менеджмента",
    kind: "lecture",
    format: "online",
    teacher: "Орлов Д. С.",
    course: "Продуктовый менеджмент",
    source: "cu",
    group: "Продукт 26/27",
    lesson: 4,
    links: [{ url: "https://my.fakeuni.test/meet/product-101", label: "Подключиться к занятию" }],
    attributes: [{ label: "Платформа", value: "Видеосвязь" }],
  },
  {
    day: 0,
    at: [15, 0],
    minutes: 80,
    title: "Английский язык",
    kind: "seminar",
    format: "online",
    teacher: "Миллер К.",
    source: "hse",
    group: HSE_GROUP,
    lesson: 5,
    url: "https://lk.fakeuni.test/classroom/english",
  },
  {
    day: 1,
    at: [10, 30],
    minutes: 80,
    title: "Дискретная математика",
    kind: "lecture",
    format: "offline",
    room: "ауд. 218",
    teacher: "Крылов И. П.",
    course: "Дискретная математика",
    source: "hse",
  },
  {
    day: 1,
    at: [13, 0],
    minutes: 80,
    title: "Теория вероятностей",
    kind: "seminar",
    format: "offline",
    room: "ауд. 305",
    teacher: "Лебедев Т. М.",
    course: "Теория вероятностей",
    source: "hse",
  },
  {
    day: 1,
    at: [18, 30],
    minutes: 90,
    title: "Data Science: вводный курс",
    kind: "lecture",
    format: "online",
    teacher: "Орлов Д. С.",
    course: "Data Science",
    source: "cu",
  },
  {
    day: 2,
    at: [9, 30],
    minutes: 80,
    title: "Алгоритмы и структуры данных",
    kind: "lecture",
    format: "offline",
    room: "ауд. 412",
    teacher: "Сидорова Е. Н.",
    course: "Алгоритмы и структуры данных",
    source: "hse",
  },
  {
    day: 2,
    at: [14, 40],
    minutes: 80,
    title: "Математический анализ",
    kind: "seminar",
    format: "hybrid",
    room: "ауд. 309",
    teacher: "Петров А. В.",
    course: "Математический анализ",
    source: "hse",
  },
  {
    day: 3,
    at: [11, 10],
    minutes: 80,
    title: "Дискретная математика",
    kind: "seminar",
    format: "offline",
    room: "ауд. 218",
    teacher: "Крылов И. П.",
    course: "Дискретная математика",
    source: "hse",
  },
  {
    day: 3,
    at: [16, 20],
    minutes: 80,
    title: "Теория вероятностей",
    kind: "lecture",
    format: "offline",
    room: "ауд. 101",
    teacher: "Лебедев Т. М.",
    course: "Теория вероятностей",
    source: "hse",
  },
  {
    day: 3,
    at: [16, 30],
    minutes: 60,
    title: "Product Studio",
    kind: "consultation",
    format: "online",
    teacher: "Орлов Д. С.",
    course: "Продуктовый менеджмент",
    source: "cu",
  },
  {
    day: 4,
    at: [10, 30],
    minutes: 80,
    title: "Консультация по курсовой",
    kind: "consultation",
    format: "offline",
    room: "ауд. 520",
    teacher: "Сидорова Е. Н.",
    source: "hse",
  },
  {
    day: 4,
    at: [14, 0],
    minutes: 120,
    title: "Зачёт по английскому языку",
    kind: "exam",
    format: "offline",
    room: "ауд. 118",
    teacher: "Миллер К.",
    source: "hse",
  },
];

/** Пересечения между разными вузами (FR-013): касание концов не конфликт — как на сервере. */
export function markConflicts(events: ScheduleEvent[]): void {
  const ordered = [...events].sort(
    (a, b) =>
      a.startsAt.localeCompare(b.startsAt) || a.endsAt.localeCompare(b.endsAt) || a.id.localeCompare(b.id),
  );
  let active: ScheduleEvent[] = [];
  for (const event of ordered) {
    active = active.filter((other) => other.endsAt > event.startsAt);
    for (const other of active) {
      if (other.provenance.spaceId !== event.provenance.spaceId) {
        event.conflictsWith.push(other.id);
        other.conflictsWith.push(event.id);
      }
    }
    active.push(event);
  }
  for (const event of events) event.conflictsWith.sort();
}

/** Занятия на период от -7 до +28 дней от сегодня. */
export function seedEvents(db: MockDb): ScheduleEvent[] {
  const today = startOfDay(db.now(), MOCK_ZONE);
  const events: ScheduleEvent[] = [];
  for (let offset = -7; offset <= 28; offset++) {
    const cycleDay = ((offset % 7) + 7) % 7;
    const dayStart = addDays(today, offset, MOCK_ZONE);
    for (const [index, template] of EVENT_TEMPLATES.entries()) {
      if (template.day !== cycleDay) continue;
      const startsAt = dayStart + (template.at[0] * 60 + template.at[1]) * 60_000;
      const weekStart = weekOf(dayStart, MOCK_ZONE).start;
      const prov = provenance(
        db,
        template.source,
        "schedule",
        "/schedule",
        `?begin_date=${new Date(weekStart + 12 * HOUR).toISOString().slice(0, 10)}`,
      );
      if (!prov) continue;
      events.push({
        id: `evt-${template.source}-${offset}-${index}`,
        title: template.title,
        kind: template.kind,
        kindText: template.kindText ?? null,
        startsAt: new Date(startsAt).toISOString(),
        endsAt: new Date(startsAt + template.minutes * 60_000).toISOString(),
        format: template.format,
        location: template.room ?? null,
        address: template.address ?? null,
        group: template.group ?? null,
        lessonNumber: template.lesson ?? null,
        teacher: template.teacher ?? null,
        courseTitle: template.course ?? null,
        links: [...(template.links ?? [])],
        attributes: [...(template.attributes ?? [])],
        url: template.url ?? null,
        changed: Boolean(template.changed) && offset >= 0 && offset < 7,
        conflictsWith: [],
        provenance: prov,
        sources: [
          {
            connectionId: prov.connectionId,
            title: db.connections.find((c) => c.id === prov.connectionId)?.title ?? "Источник",
          },
        ],
        disagreement: null,
      });
    }
  }
  markConflicts(events);
  return events;
}

// --- сроки ---------------------------------------------------------------------------------------

interface DeadlineTemplate {
  title: string;
  course: string | null;
  source: SeedSource;
  /** Срок в часах от «сейчас» или null — срока нет. */
  dueInHours: number | null;
  /** Срок, назначенный преподавателем (сдвиг от базового), в часах от «сейчас». */
  effectiveInHours?: number;
  state: S["DeadlineOut"]["state"];
  /** Статус словами источника. */
  stateText?: string;
  /** Статус выведен правилом студента (D-045), а не словарём. */
  byRule?: boolean;
  /** Когда статус сменился, в часах назад: попадает в «Новое». */
  changedHoursAgo?: number;
  /** Оценка, присоединённая к проверенному заданию. */
  grade?: { value: string; scale: string | null; valueNum: number | null; scaleMax: number | null };
}

const DEADLINE_TEMPLATES: readonly DeadlineTemplate[] = [
  {
    title: "Домашнее задание №4: ряды",
    course: "Математический анализ",
    source: "hse",
    dueInHours: 5,
    state: "open",
  },
  {
    title: "Лабораторная: алгоритмы сортировки",
    course: "Алгоритмы и структуры данных",
    source: "hse",
    dueInHours: 72,
    effectiveInHours: 48,
    state: "open",
    stateText: "На доработке",
    changedHoursAgo: 20,
  },
  {
    title: "Эссе по продуктовой стратегии",
    course: "Продуктовый менеджмент",
    source: "cu",
    dueInHours: 96,
    effectiveInHours: 120,
    state: "open",
  },
  {
    title: "Контрольная работа по дискретной математике",
    course: "Дискретная математика",
    source: "hse",
    dueInHours: 216,
    state: "open",
  },
  {
    title: "Проект: прототип сервиса",
    course: "Data Science",
    source: "cu",
    dueInHours: 24 * 24,
    state: "open",
  },
  {
    title: "Прочитать главы 3–4",
    course: "Продуктовый менеджмент",
    source: "cu",
    dueInHours: null,
    state: "open",
  },
  { title: "Курсовая: выбор темы", course: null, source: "hse", dueInHours: -5, state: "open" },
  {
    title: "Домашнее задание №3: пределы",
    course: "Математический анализ",
    source: "hse",
    dueInHours: -72,
    state: "submitted",
    stateText: "Решение прикреплено",
    byRule: true,
    changedHoursAgo: 30,
  },
  {
    title: "Тест по теории вероятностей",
    course: "Теория вероятностей",
    source: "hse",
    dueInHours: -144,
    state: "graded",
    stateText: "Проверено",
    grade: { value: "10", scale: "10 баллов", valueNum: 10, scaleMax: 10 },
  },
  { title: "Реферат по истории науки", course: null, source: "cu", dueInHours: -48, state: "missed" },
  {
    title: "Кейс по метрикам продукта",
    course: "Продуктовый менеджмент",
    source: "cu",
    dueInHours: -24 * 28,
    state: "graded",
    stateText: "Оценено",
    grade: { value: "78", scale: "100 баллов", valueNum: 78, scaleMax: 100 },
  },
  {
    title: "Проектная работа, этап 2",
    course: "Продуктовый менеджмент",
    source: "cu",
    dueInHours: -24 * 3,
    state: "graded",
    stateText: "Оценено",
    changedHoursAgo: 46,
    grade: { value: "95", scale: "100 баллов", valueNum: 95, scaleMax: 100 },
  },
  // Источник статуса не знает (срок прошёл), студент отметил «сдал» (D-045): `db.deadlineMarks["dl-12"]`.
  {
    title: "Конспект лекции 5",
    course: "Data Science",
    source: "cu",
    dueInHours: -30,
    state: "open",
  },
];

export function seedDeadlines(db: MockDb): Deadline[] {
  const now = db.now();
  const rows: Deadline[] = [];
  for (const [index, template] of DEADLINE_TEMPLATES.entries()) {
    const prov = provenance(db, template.source, "portal", "/tasks");
    if (!prov) continue;
    const at = (hours: number | undefined) =>
      hours === undefined ? null : new Date(now + hours * HOUR).toISOString();
    const id = `dl-${index}`;
    const mark = db.deadlineMarks?.[id];
    // Правило студента переводит подпись источника в состояние; без правила словарь подпись не знает.
    const ruled =
      template.byRule === true &&
      (db.statusRules ?? []).some((rule) => rule.label.toLowerCase() === template.stateText?.toLowerCase());
    const sourceState = template.byRule && !ruled ? "unknown" : template.state;
    const url = `${prov.openUrl}/${index}`;
    rows.push({
      id,
      title: template.title,
      dueAt: at(template.dueInHours ?? undefined),
      effectiveDueAt: at(template.effectiveInHours),
      // Итоговое состояние, как у сервера: отметка студента → правило → источник (D-045).
      state: mark ?? sourceState,
      stateText: template.stateText ?? null,
      stateSource: mark ? "student" : ruled ? "rule" : "source",
      changedAt:
        template.changedHoursAgo === undefined
          ? null
          : new Date(now - template.changedHoursAgo * HOUR).toISOString(),
      grade: template.grade ? { ...template.grade, url: `${url}/grade` } : null,
      courseTitle: template.course,
      url,
      provenance: prov,
    });
  }
  return rows;
}

// --- предметы, оценки, люди, новости, документы ---------------------------------------------------

const COURSES: readonly { title: string; code: string; term: string; source: SeedSource }[] = [
  { title: "Математический анализ", code: "МА-101", term: "Осень 2026", source: "hse" },
  { title: "Алгоритмы и структуры данных", code: "АСД-201", term: "Осень 2026", source: "hse" },
  { title: "Дискретная математика", code: "ДМ-102", term: "Осень 2026", source: "hse" },
  { title: "Теория вероятностей", code: "ТВ-202", term: "Осень 2026", source: "hse" },
  { title: "Продуктовый менеджмент", code: "PM-1", term: "Осень 2026", source: "cu" },
  { title: "Data Science", code: "DS-1", term: "Осень 2026", source: "cu" },
];

export function seedCourses(db: MockDb): Course[] {
  return COURSES.flatMap((course, index) => {
    const prov = provenance(db, course.source, "portal", "/courses");
    return prov
      ? [
          {
            id: `course-${index}`,
            title: course.title,
            code: course.code,
            term: course.term,
            url: `${prov.openUrl}/${index}`,
            provenance: prov,
          },
        ]
      : [];
  });
}

interface GradeTemplate {
  title: string | null;
  course: string | null;
  value: string;
  scale: string | null;
  weight: number | null;
  daysAgo: number | null;
  source: SeedSource;
  /** Слой D-043; без него — оценка за работу. */
  layer?: GradeLayer;
  /** Верх шкалы числом, когда источник дал его отдельно; иначе выводится из «10 баллов». */
  max?: number;
  period?: string;
  comment?: string;
}

/**
 * Оценки синтетического вуза по слоям D-043: работы (10-балльная шкала ВШЭ и 100-балльная БРС ЦУ), накопленное по
 * предмету, итоги в зачётке за прошлые периоды и сводные показатели. Значения — как «напечатал источник».
 */
const GRADES: readonly GradeTemplate[] = [
  {
    title: "Контрольная №2",
    course: "Математический анализ",
    value: "8",
    scale: "10 баллов",
    weight: 0.2,
    daysAgo: 1,
    source: "hse",
    comment:
      "Хорошее решение. В задаче 3 не обоснован переход к пределу — посмотрите теорему о двух милиционерах.",
  },
  {
    title: "Домашнее задание №2",
    course: "Алгоритмы и структуры данных",
    value: "9",
    scale: "10 баллов",
    weight: 0.1,
    daysAgo: 6,
    source: "hse",
  },
  {
    title: "Коллоквиум",
    course: "Дискретная математика",
    value: "7",
    scale: "10 баллов",
    weight: 0.3,
    daysAgo: 12,
    source: "hse",
  },
  {
    title: "Тест по теории вероятностей",
    course: "Теория вероятностей",
    value: "10",
    scale: "10 баллов",
    weight: 0.1,
    daysAgo: 20,
    source: "hse",
  },
  {
    title: "Проектная работа, этап 1",
    course: "Продуктовый менеджмент",
    value: "86",
    scale: "100 баллов",
    weight: null,
    daysAgo: 8,
    source: "cu",
  },
  {
    title: "Вводный тест",
    course: "Data Science",
    value: "92",
    scale: "100 баллов",
    weight: null,
    daysAgo: 15,
    source: "cu",
  },
  // Дальше — оценки прошлых недель: на них строятся графики панелей «Успеваемости».
  {
    title: "Контрольная №1",
    course: "Дискретная математика",
    value: "7",
    scale: "10 баллов",
    weight: 0.2,
    daysAgo: 26,
    source: "hse",
  },
  {
    title: "Домашнее задание №1",
    course: "Дискретная математика",
    value: "8",
    scale: "10 баллов",
    weight: 0.1,
    daysAgo: 31,
    source: "hse",
  },
  {
    title: "Входной тест",
    course: "Теория вероятностей",
    value: "9",
    scale: "10 баллов",
    weight: null,
    daysAgo: 36,
    source: "hse",
  },
  {
    title: "Кейс по метрикам продукта",
    course: "Продуктовый менеджмент",
    value: "78",
    scale: "100 баллов",
    weight: null,
    daysAgo: 27,
    source: "cu",
  },
  {
    title: "Практикум по Python",
    course: "Data Science",
    value: "88",
    scale: "100 баллов",
    weight: null,
    daysAgo: 22,
    source: "cu",
  },
  {
    title: "Проектная работа, этап 2",
    course: "Продуктовый менеджмент",
    value: "95",
    scale: "100 баллов",
    weight: null,
    daysAgo: 2,
    source: "cu",
  },
  {
    title: "Участие на семинарах",
    course: "Алгоритмы и структуры данных",
    value: "зачтено",
    scale: null,
    weight: null,
    daysAgo: null,
    source: "hse",
  },
  // Работы и накопленное по матанализу: 10-балльная шкала, итог курса считает источник, а не приложение.
  {
    title: "Домашнее задание №1",
    course: "Математический анализ",
    value: "9",
    scale: "10 баллов",
    weight: 0.1,
    daysAgo: 24,
    source: "hse",
  },
  {
    title: "Домашнее задание №2",
    course: "Математический анализ",
    value: "7,5",
    scale: "10 баллов",
    weight: 0.1,
    daysAgo: 14,
    source: "hse",
  },
  {
    title: "Коллоквиум",
    course: "Математический анализ",
    value: "6",
    scale: "10 баллов",
    weight: 0.2,
    daysAgo: 9,
    source: "hse",
  },
  {
    title: "Накопленная оценка",
    course: "Математический анализ",
    value: "7,6",
    scale: "10 баллов",
    weight: null,
    daysAgo: 1,
    source: "hse",
    layer: "current",
    period: "1 модуль",
  },
  {
    title: "Накопленная оценка",
    course: "Алгоритмы и структуры данных",
    value: "8,8",
    scale: "10 баллов",
    weight: null,
    daysAgo: 6,
    source: "hse",
    layer: "current",
    period: "1 модуль",
  },
  {
    title: "Итог за модуль",
    course: "Теория вероятностей",
    value: "зачтено",
    scale: "зачёт",
    weight: null,
    daysAgo: 2,
    source: "hse",
    layer: "final",
    period: "1 модуль 2026/27",
  },
  // Зачётка прошлых периодов: предметов в текущих работах нет, они живут только в «Зачётке».
  {
    title: null,
    course: "Линейная алгебра",
    value: "9",
    scale: "10 баллов",
    weight: null,
    daysAgo: 160,
    source: "hse",
    layer: "final",
    period: "2025/26, весна",
  },
  {
    title: null,
    course: "Английский язык",
    value: "отлично",
    scale: null,
    weight: null,
    daysAgo: 160,
    source: "hse",
    layer: "final",
    period: "2025/26, весна",
  },
  {
    title: null,
    course: "История России",
    value: "зачтено",
    scale: "зачёт",
    weight: null,
    daysAgo: 300,
    source: "hse",
    layer: "final",
    period: "2025/26, осень",
  },
  {
    title: null,
    course: "Программирование на Python",
    value: "10",
    scale: "10 баллов",
    weight: null,
    daysAgo: 300,
    source: "hse",
    layer: "final",
    period: "2025/26, осень",
  },
  {
    title: "Средний балл",
    course: null,
    value: "8,62",
    scale: null,
    weight: null,
    daysAgo: 160,
    source: "hse",
    layer: "summary",
    max: 10,
    period: "2025/26",
  },
  {
    title: "Кредиты",
    course: null,
    value: "60",
    scale: "кредитов",
    weight: null,
    daysAgo: 160,
    source: "hse",
    layer: "summary",
    period: "2025/26",
  },
  // ЦУ: 100-балльная БРС, «баллы за курс» — накопленное источника.
  {
    title: "Баллы за курс",
    course: "Продуктовый менеджмент",
    value: "64",
    scale: "баллы за курс",
    weight: null,
    daysAgo: 2,
    source: "cu",
    layer: "current",
    max: 100,
    period: "Осень 2026",
  },
  {
    title: "GPA",
    course: null,
    value: "4,6",
    scale: null,
    weight: null,
    daysAgo: 40,
    source: "cu",
    layer: "summary",
    max: 5,
    period: "1 семестр",
  },
];

/**
 * Оценки «как у всех сегодня» (вариант `?data=legacy`): старые playbook'и слоёв не знают. Синтетика по форме
 * production, без чьих-либо данных.
 *
 * - A: оценка за курс — слоя нет, `courseTitle` нет (`courseId` есть, но предмета с ним нет), название курса в `title`,
 *   значение «9,41» или «2», в `scale` — подпись, а не шкала («оценка LMS», «баллы за курс»), чисел нет, даты нет.
 *   Две записи одного курса с разными значениями — обычное дело.
 * - B: работа с баллом без предмета: «ДЗ 3. …», «19» «из 20», `valueNum`/`scaleMax`, слоя нет.
 * - Первый вуз — только A; второй — A и B, плюс работы без числа («зачтено») с `assignmentId`.
 */
interface LegacyTemplate {
  title: string;
  value: string;
  scale: string | null;
  source: SeedSource;
  courseId?: string;
  numbers?: { valueNum: number; scaleMax: number };
  assignmentId?: string;
  daysAgo?: number;
}

const LEGACY_GRADES: readonly LegacyTemplate[] = [
  {
    title: "Алгоритмы и структуры данных 1 (2025/2026 модули: 2,3)",
    value: "9,41",
    scale: "оценка LMS",
    source: "hse",
    courseId: "lms-1",
  },
  {
    title: "Безопасность жизнедеятельности (2025/2026 модули: 1,2)",
    value: "5",
    scale: "оценка LMS",
    source: "hse",
    courseId: "lms-2",
  },
  {
    title: "Безопасность жизнедеятельности (2025/2026 модули: 1,2)",
    value: "5,44",
    scale: "оценка LMS",
    source: "hse",
    courseId: "lms-3",
  },
  {
    title: "История России (2025/2026 модули: 1,2,3,4)",
    value: "0,00",
    scale: "оценка LMS",
    source: "hse",
    courseId: "lms-4",
  },
  {
    title: "История России (2025/2026 модули: 1,2,3,4)",
    value: "2,37",
    scale: "оценка LMS",
    source: "hse",
    courseId: "lms-5",
  },
  {
    title: "Курсовой проект, 2 курс 26-27 уч. г.",
    value: "2,00",
    scale: "оценка LMS",
    source: "hse",
    courseId: "lms-6",
  },
  {
    title: "(Курс) Гибкие методологии разработки",
    value: "0",
    scale: "баллы за курс",
    source: "cu",
    courseId: "c-1",
  },
  {
    title: "(Курс) Методы и системы обработки больших данных",
    value: "2",
    scale: "баллы за курс",
    source: "cu",
    courseId: "c-2",
  },
  {
    title: "ДЗ 3. Линейная регрессия и регуляризация",
    value: "19",
    scale: "из 20",
    source: "cu",
    numbers: { valueNum: 19, scaleMax: 20 },
    daysAgo: 4,
  },
  {
    title: "ДЗ 4. Деревья решений",
    value: "17",
    scale: "из 20",
    source: "cu",
    numbers: { valueNum: 17, scaleMax: 20 },
    daysAgo: 2,
  },
  {
    title: "Семинар 5: устный опрос",
    value: "зачтено",
    scale: "зачёт",
    source: "cu",
    assignmentId: "a-5",
    daysAgo: 6,
  },
  {
    title: "Семинар 6: устный опрос",
    value: "зачтено",
    scale: "зачёт",
    source: "cu",
    assignmentId: "a-6",
    daysAgo: 5,
  },
  {
    title: "Семинар 7: устный опрос",
    value: "не зачтено",
    scale: "зачёт",
    source: "cu",
    assignmentId: "a-7",
    daysAgo: 3,
  },
];

export function seedLegacyGrades(db: MockDb): Grade[] {
  return LEGACY_GRADES.flatMap((grade, index) => {
    const prov = provenance(db, grade.source, "portal", "/grades");
    if (!prov) return [];
    const at =
      grade.daysAgo === undefined ? null : new Date(db.now() - grade.daysAgo * 24 * HOUR).toISOString();
    return [
      {
        id: `legacy-${index}`,
        title: grade.title,
        value: grade.value,
        scale: grade.scale,
        weight: null,
        courseTitle: null,
        courseId: grade.courseId ?? null,
        gradedAt: at,
        valueNum: grade.numbers?.valueNum ?? null,
        scaleMax: grade.numbers?.scaleMax ?? null,
        layer: null,
        period: null,
        assignmentId: grade.assignmentId ?? null,
        comment: null,
        url: null,
        changedAt: at,
        provenance: prov,
      },
    ];
  });
}

/** Число и максимум, если источник дал их однозначно: явный `max` или «10 баллов». Иначе — ничего. */
function gradeNumbers(template: GradeTemplate): { valueNum: number | null; scaleMax: number | null } {
  const valueNum = Number(template.value.replace(",", "."));
  const max = template.max ?? (template.scale ? Number((template.scale.match(/\d+/) ?? [])[0]) : Number.NaN);
  return Number.isFinite(valueNum) && Number.isFinite(max) && valueNum <= max
    ? { valueNum, scaleMax: max }
    : { valueNum: null, scaleMax: null };
}

export function seedGrades(db: MockDb): Grade[] {
  return GRADES.flatMap((grade, index) => {
    const prov = provenance(db, grade.source, "portal", "/grades");
    return prov
      ? [
          {
            id: `grade-${index}`,
            title: grade.title,
            value: grade.value,
            scale: grade.scale,
            weight: grade.weight,
            courseTitle: grade.course,
            gradedAt:
              grade.daysAgo === null ? null : new Date(db.now() - grade.daysAgo * 24 * HOUR).toISOString(),
            ...gradeNumbers(grade),
            layer: grade.layer ?? "item",
            period: grade.period ?? null,
            comment: grade.comment ?? null,
            url: `${prov.openUrl}/${index}`,
            // Изменение видно в «Новом», если оно случилось за последний месяц (первая синхронизация — раньше).
            changedAt:
              grade.daysAgo !== null && grade.daysAgo <= 30
                ? new Date(db.now() - grade.daysAgo * 24 * HOUR).toISOString()
                : null,
            provenance: prov,
          },
        ]
      : [];
  });
}

const PEOPLE: readonly { name: string; role: string; email: string | null; source: SeedSource }[] = [
  {
    name: "Петров Александр Владимирович",
    role: "Лектор, математический анализ",
    email: "petrov@fakeuni.test",
    source: "hse",
  },
  {
    name: "Сидорова Елена Николаевна",
    role: "Преподаватель, алгоритмы",
    email: "sidorova@fakeuni.test",
    source: "hse",
  },
  { name: "Крылов Игорь Петрович", role: "Преподаватель, дискретная математика", email: null, source: "hse" },
  {
    name: "Лебедев Тимур Маратович",
    role: "Преподаватель, теория вероятностей",
    email: "lebedev@fakeuni.test",
    source: "hse",
  },
  {
    name: "Орлов Дмитрий Сергеевич",
    role: "Куратор программы",
    email: "orlov@my.fakeuni.test",
    source: "cu",
  },
];

export function seedPeople(db: MockDb): Person[] {
  return PEOPLE.flatMap((person, index) => {
    const prov = provenance(db, person.source, "portal", "/people");
    return prov
      ? [
          {
            id: `person-${index}`,
            name: person.name,
            role: person.role,
            email: person.email,
            provenance: prov,
          },
        ]
      : [];
  });
}

const NEWS: readonly { title: string; body: string | null; daysAgo: number; source: SeedSource }[] = [
  {
    title: "Изменения в расписании на следующей неделе",
    body: "Лекция по математическому анализу в среду переносится в аудиторию 309. Время не меняется.",
    daysAgo: 1,
    source: "hse",
  },
  {
    title: "Открыта запись на факультативы",
    body: "Запись открыта до пятницы. Выбрать факультатив можно в личном кабинете.",
    daysAgo: 4,
    source: "hse",
  },
  { title: "Хакатон по продуктовой аналитике", body: null, daysAgo: 9, source: "cu" },
];

export function seedNews(db: MockDb): NewsItem[] {
  return NEWS.flatMap((item, index) => {
    const prov = provenance(db, item.source, "portal", "/news");
    return prov
      ? [
          {
            id: `news-${index}`,
            title: item.title,
            body: item.body,
            publishedAt: new Date(db.now() - item.daysAgo * 24 * HOUR).toISOString(),
            url: `${prov.openUrl}/${index}`,
            provenance: prov,
          },
        ]
      : [];
  });
}

export function seedDocuments(db: MockDb): S["DocumentOut"][] {
  const space = db.spaces.find((row) => seedSourceOf(db, row.id) === "hse");
  if (!space) return [];
  const connection = db.connections.find((c) => c.spaceId === space.id && c.kind === "portal") ?? null;
  const at = (hours: number) => new Date(db.now() - hours * HOUR).toISOString();
  return [
    {
      id: "doc-0",
      spaceId: space.id,
      connectionId: connection?.id ?? null,
      kind: "file",
      title: "Программа курса: математический анализ.pdf",
      url: "https://lk.fakeuni.test/files/ma-program.pdf",
      mime: "application/pdf",
      fetchedAt: at(30),
    },
    {
      id: "doc-1",
      spaceId: space.id,
      connectionId: connection?.id ?? null,
      kind: "sheet",
      title: "Рейтинг группы, осень",
      url: "https://docs.fakeuni.test/sheets/rating",
      mime: null,
      fetchedAt: at(52),
    },
    {
      id: "doc-2",
      spaceId: space.id,
      connectionId: connection?.id ?? null,
      kind: "page",
      title: "Правила сдачи курсовых работ",
      url: "https://lk.fakeuni.test/rules/coursework",
      mime: "text/html",
      fetchedAt: at(100),
    },
  ];
}

// --- главная ----------------------------------------------------------------------------------

export function dueOf(row: Deadline): number | null {
  const value = row.effectiveDueAt ?? row.dueAt;
  return value ? Date.parse(value) : null;
}

// --- «Новое» ------------------------------------------------------------------------------------------

const DAY = 24 * HOUR;

/**
 * «Новое» (D-044 §5.5) из тех же сидов: оценки и задания, у которых `changedAt` попал в окно `days`, свежие первыми.
 * Форма — `StudyUpdate` (контракт потока A).
 */
export function seedUpdates(db: MockDb, days: number, allGrades: Grade[] = seedGrades(db)): StudyUpdate[] {
  const since = db.now() - days * DAY;
  const fresh = (at: string | null | undefined): at is string =>
    Boolean(at) && Date.parse(at as string) >= since;
  const grades = allGrades
    .filter((grade) => fresh(grade.changedAt))
    .map(
      (grade): StudyUpdate => ({
        kind: "grade",
        id: grade.id,
        title: grade.title,
        courseTitle: grade.courseTitle,
        value: grade.value,
        scale: grade.scale,
        valueNum: grade.valueNum ?? null,
        scaleMax: grade.scaleMax ?? null,
        layer: grade.layer ?? null,
        state: null,
        stateText: null,
        url: grade.url ?? null,
        changedAt: grade.changedAt as string,
        provenance: grade.provenance,
      }),
    );
  const deadlines = seedDeadlines(db)
    .filter((deadline) => fresh(deadline.changedAt))
    .map(
      (deadline): StudyUpdate => ({
        kind: "deadline",
        id: deadline.id,
        title: deadline.title,
        courseTitle: deadline.courseTitle,
        value: deadline.grade?.value ?? null,
        scale: deadline.grade?.scale ?? null,
        valueNum: deadline.grade?.valueNum ?? null,
        scaleMax: deadline.grade?.scaleMax ?? null,
        layer: null,
        state: deadline.state,
        stateText: deadline.stateText ?? null,
        url: deadline.url,
        changedAt: deadline.changedAt as string,
        provenance: deadline.provenance,
      }),
    );
  return [...grades, ...deadlines].sort(
    (a, b) => Date.parse(b.changedAt) - Date.parse(a.changedAt) || a.id.localeCompare(b.id),
  );
}

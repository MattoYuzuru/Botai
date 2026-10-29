import { formatTime, type IconName, safeHttpUrl } from "@botai/design";
import type { EventAttribute, ScheduleEvent } from "@/api/types";
import type { EventFormat, EventKind } from "@/features/data/types";

export const EVENT_KIND_LABEL: Record<EventKind, string | null> = {
  lecture: "Лекция",
  seminar: "Семинар",
  lab: "Лабораторная",
  exam: "Экзамен",
  consultation: "Консультация",
  other: null,
};

export const EVENT_FORMAT_LABEL: Record<EventFormat, string | null> = {
  offline: "Очно",
  online: "Онлайн",
  hybrid: "Смешанный формат",
  unknown: null,
};

/**
 * Что нужно пресету `eventDetails`: начало и конец, остальное необязательно. `ScheduleEvent` подходит, но
 * пресет не привязан к нему — любая «пара» с такими полями (занятие кружка, пересдача) получит ту же карточку.
 */
export type EventDetailsSource = Pick<ScheduleEvent, "startsAt" | "endsAt"> &
  Partial<
    Pick<
      ScheduleEvent,
      | "kind"
      | "kindText"
      | "format"
      | "location"
      | "address"
      | "teacher"
      | "group"
      | "courseTitle"
      | "lessonNumber"
      | "links"
      | "attributes"
    >
  >;

export interface EventFieldContext {
  timeZone?: string;
}

/** Строка реестра полей: ключ, иконка, подпись и способ получить текст из события. */
export interface EventFieldDef {
  key: string;
  icon: IconName;
  label: string;
  /** Нужна ли строка в компактной карточке пары (список дня и колонка недели). */
  compact: boolean;
  /** Текст поля; `null` — у источника такого поля нет, и строка не показывается. */
  text: (event: EventDetailsSource, context: EventFieldContext) => string | null;
}

/** Пустую строку, пробелы по краям и повторные пробелы источник присылает как есть — приводим к виду без них. */
const clean = (value: string | null | undefined): string | null => {
  const text = value?.trim().replace(/\s+/g, " ");
  return text ? text : null;
};

const parse = (value: string): number => Date.parse(value);

/**
 * Реестр полей карточки пары. Порядок — порядок показа. Новое поле — новая строка здесь: иконка, подпись и
 * формат живут вместе, а карточка в списке, в сетке и в шите подхватывает её без правок разметки.
 * Иконки — набор дизайн-системы: адрес — геометка, аудитория — дверь, кампус или здание — учебное здание.
 */
export const EVENT_FIELDS: readonly EventFieldDef[] = [
  {
    key: "time",
    icon: "time",
    label: "Время",
    compact: false,
    text: (event, { timeZone }) =>
      `${formatTime(parse(event.startsAt), timeZone)}–${formatTime(parse(event.endsAt), timeZone)}`,
  },
  {
    key: "lesson",
    icon: "number",
    label: "Номер пары",
    compact: false,
    text: (event) => (event.lessonNumber ? `${event.lessonNumber}-я пара` : null),
  },
  { key: "room", icon: "room", label: "Аудитория", compact: true, text: (event) => clean(event.location) },
  { key: "address", icon: "address", label: "Адрес", compact: true, text: (event) => clean(event.address) },
  {
    key: "teacher",
    icon: "teacher",
    label: "Преподаватель",
    compact: true,
    text: (event) => clean(event.teacher),
  },
  { key: "group", icon: "group", label: "Группа", compact: true, text: (event) => clean(event.group) },
  {
    key: "course",
    icon: "course",
    label: "Предмет",
    compact: false,
    text: (event) => clean(event.courseTitle),
  },
];

/** Иконки, которые источник может подсказать в `attributes[].icon`. Остальные имена игнорируются. */
const ATTRIBUTE_ICONS: Readonly<Record<string, IconName>> = {
  building: "building",
  campus: "building",
  address: "address",
  room: "room",
  teacher: "teacher",
  group: "group",
  time: "time",
  number: "number",
  course: "course",
  info: "info",
};

/** Подпись → иконка, когда источник иконку не назвал: «Кампус», «Корпус», «Здание» — учебное здание. */
const ATTRIBUTE_LABELS: readonly (readonly [RegExp, IconName])[] = [
  [/кампус|корпус|здани|building|campus/i, "building"],
  [/адрес|address/i, "address"],
  [/аудитор|кабинет|room/i, "room"],
  [/преподавател|лектор|teacher/i, "teacher"],
  [/групп|поток|group/i, "group"],
];

/** Иконка атрибута: подсказка источника, иначе по подписи, иначе общая «информация». */
export function attributeIcon(attribute: Pick<EventAttribute, "label" | "icon">): IconName {
  const hint = attribute.icon?.trim().toLowerCase();
  if (hint && hint in ATTRIBUTE_ICONS) return ATTRIBUTE_ICONS[hint] as IconName;
  return ATTRIBUTE_LABELS.find(([pattern]) => pattern.test(attribute.label))?.[1] ?? "info";
}

/** Одна показанная строка: из реестра или из `attributes`. */
export interface DetailFact {
  key: string;
  icon: IconName;
  label: string;
  text: string;
  compact: boolean;
}

/** Онлайн-ссылка занятия, прошедшая проверку схемы: открывается только http(s). */
export interface EventLinkView {
  href: string;
  label: string;
}

export interface EventDetails {
  /** Тип занятия бейджем: слова источника («Текущий рубежный контроль»), иначе наш вид («Лекция»). */
  badge: string | null;
  /** Строки в порядке показа: поля реестра, затем атрибуты. Только те, что есть. */
  facts: DetailFact[];
  links: EventLinkView[];
}

/** Подпись ссылки: подпись источника, иначе хост. */
function linkLabel(label: string | null | undefined, url: URL): string {
  return clean(label) ?? url.hostname.replace(/^www\./, "");
}

/**
 * Пресет `eventDetails`: что показать в карточке пары. Берёт то, что есть: пустые поля пропускаются, а
 * атрибуты с тем же текстом, что уже показан полем, не повторяются. Один результат кормит и шит (все
 * строки и ссылки), и компактную карточку (`compactFacts`).
 */
export function eventDetails(event: EventDetailsSource, context: EventFieldContext = {}): EventDetails {
  const facts: DetailFact[] = [];
  for (const field of EVENT_FIELDS) {
    const text = field.text(event, context);
    if (text)
      facts.push({ key: field.key, icon: field.icon, label: field.label, text, compact: field.compact });
  }
  const seen = new Set(facts.map((fact) => fact.text.toLowerCase()));
  for (const [index, attribute] of (event.attributes ?? []).entries()) {
    const label = clean(attribute.label);
    const text = clean(attribute.value);
    if (!label || !text || seen.has(text.toLowerCase())) continue;
    seen.add(text.toLowerCase());
    const icon = attributeIcon({ label, icon: attribute.icon });
    // Компактная карточка берёт из атрибутов только учебное здание: остальное — в шите.
    const fact: DetailFact = { key: `attribute-${index}`, icon, label, text, compact: icon === "building" };
    // Здание стоит рядом с адресом и аудиторией (после них и после уже поставленных зданий), остальное — в конце.
    const place = fact.compact
      ? facts.findLastIndex((row) => row.key === "address" || row.key === "room" || row.icon === "building")
      : -1;
    facts.splice(place === -1 ? facts.length : place + 1, 0, fact);
  }
  const links = (event.links ?? []).flatMap((link): EventLinkView[] => {
    const url = safeHttpUrl(link.url);
    return url ? [{ href: url.href, label: linkLabel(link.label, url) }] : [];
  });
  const badge = clean(event.kindText) ?? (event.kind ? EVENT_KIND_LABEL[event.kind] : null);
  return { badge, facts, links };
}

/** Строки компактной карточки пары: аудитория, адрес, преподаватель, группа и здание. */
export const compactFacts = (details: EventDetails): DetailFact[] =>
  details.facts.filter((fact) => fact.compact);

/**
 * Расширения контракта данных агент-хаба v3 (docs/plan/agent-hub-v3.md §3.5, §3.7), объявленные вручную:
 * бэкенд меняется параллельно, а `schema.gen.ts` перегенерируется только на интеграции. Каждый тип берёт
 * сгенерированный и подменяет поля расширения (`Omit<…> & Ext`), поэтому после перегенерации схемы код не
 * меняется, а `asV3` становится тождественным.
 */
import type { components } from "./schema.gen";

type Schemas = components["schemas"];

/** Онлайн-ссылка занятия: «Подключиться», запись лекции, материалы. Подпись задал источник. */
export interface EventLink {
  url: string;
  label: string;
}

/** Дополнительный атрибут занятия из источника; подпись общая (без личных значений), `icon` — подсказка. */
export interface EventAttribute {
  label: string;
  value: string;
  icon?: string | null;
}

interface ProvenanceExt {
  /** Страница интерфейса, где видна запись (`/schedule?begin_date=…`). Адрес API (`sourceUrl`) — только провенанс. */
  openUrl: string | null;
}

export type Provenance = Omit<Schemas["Provenance"], keyof ProvenanceExt> & ProvenanceExt;

interface WithProvenance {
  provenance: Provenance;
}

type Widen<T extends { provenance: Schemas["Provenance"] }> = Omit<T, "provenance"> & WithProvenance;

interface ScheduleEventExt extends WithProvenance {
  /** Тип занятия словами источника: «Текущий рубежный контроль». */
  kindText: string | null;
  /** Адрес здания: «Покровский б-р, д.11». */
  address: string | null;
  /** Группа или поток: «ДРИП 3 курс 26/27». */
  group: string | null;
  lessonNumber: number | null;
  links: EventLink[];
  attributes: EventAttribute[];
  /** Страница интерфейса источника с этой парой. */
  openUrl: string | null;
}

export type ScheduleEvent = Omit<Schemas["ScheduleEventOut"], keyof ScheduleEventExt> & ScheduleEventExt;
export type Deadline = Widen<Schemas["DeadlineOut"]>;
export type Course = Widen<Schemas["CourseOut"]>;
export type Grade = Widen<Schemas["GradeOut"]>;
export type Person = Widen<Schemas["PersonOut"]>;
export type NewsItem = Widen<Schemas["NewsOut"]>;

export type Today = Omit<Schemas["TodayOut"], "events" | "deadlines"> & {
  events: ScheduleEvent[];
  deadlines: Deadline[];
};

interface SpaceExt {
  /** «НИУ ВШЭ»: короткое имя вуза для чипов и подписей; у пространства без вуза — `null`. */
  shortTitle: string | null;
  aliases: string[];
}

export type Space = Omit<Schemas["SpaceOut"], keyof SpaceExt> & SpaceExt;

/** Вид источника вуза (§3.5). */
export type ConnectionKind =
  | "portal"
  | "lms"
  | "schedule"
  | "site"
  | "sheet"
  | "doc"
  | "document"
  | "calendar"
  | "other";

/** Фоновая задача источника; форма — как у `ThreadOut.activeTask` (§3.2). */
export interface ConnectionTask {
  jobId: string;
  kind: "connect" | "repair" | "sync" | "ingest";
  connectionId: string | null;
  state: string;
  title: string;
}

interface ConnectionExt {
  kind: ConnectionKind;
  /** UI-страница входа в данные источника, без секретных query. */
  entryUrl: string | null;
  /** Тред, где живёт задача источника: плашки статусов ведут в него. */
  threadId: string | null;
  activeTask: ConnectionTask | null;
}

export type Connection = Omit<Schemas["ConnectionOut"], keyof ConnectionExt> & ConnectionExt;

interface Result {
  data?: unknown;
  error?: unknown;
  response: Response;
}

/**
 * Ответ типизированного клиента, чья форма уже расширена v3, но ещё не попала в `schema.gen.ts`. Единственное
 * место приведения: после перегенерации схемы вызов превращается в тождество и его можно убрать.
 */
export function asV3<T>(
  request: Promise<Result>,
): Promise<{ data?: T; error?: unknown; response: Response }> {
  return request as Promise<{ data?: T; error?: unknown; response: Response }>;
}

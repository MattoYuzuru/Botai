/**
 * Расширения контракта данных агент-хаба v3 (docs/plan/agent-hub-v3.md §3.5, §3.7), объявленные вручную:
 * бэкенд меняется параллельно, а `schema.gen.ts` перегенерируется только на интеграции. Каждый тип берёт
 * сгенерированный и подменяет поля расширения (`Omit<…> & Ext`). Это утверждение формы, а не проверка:
 * на интеграции, после перегенерации схемы, типы сверяются с бэкендом, а `asV3` и `Omit<…> & Ext`
 * заменяются типами из `schema.gen.ts`.
 */
import type { components } from "./schema.gen";

type Schemas = components["schemas"];

/** Онлайн-ссылка занятия: «Подключиться», запись лекции, материалы. Подпись задал источник. */
export interface EventLink {
  url: string;
  label: string;
}

/** Дополнительный атрибут занятия из источника; подпись общая (без личных значений). Иконку выводит пресет по подписи. */
export interface EventAttribute {
  label: string;
  value: string;
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
  shortTitle?: string | null;
  aliases?: string[];
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

/** Фоновая задача источника (WS4): что делает агент сейчас; название и тред задачи в ответ не входят. */
export interface ConnectionTask {
  jobId: string;
  kind: "connect" | "repair" | "sync" | "ingest";
  state: "queued" | "running" | "waiting_user";
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
 * место приведения типа (`as`): оно ничего не проверяет в рантайме, поэтому после перегенерации схемы и
 * сверки типов с бэкендом вызовы `asV3` убираются.
 */
export function asV3<T>(
  request: Promise<Result>,
): Promise<{ data?: T; error?: unknown; response: Response }> {
  return request as Promise<{ data?: T; error?: unknown; response: Response }>;
}

/**
 * Типы данных агент-хаба v3 (docs/v4/05-implementation-plan.md §3.5, §3.7) поверх сгенерированной схемы
 * (`schema.gen.ts`, её пересобирает `scripts/gen.sh`). Бэкенд всегда присылает поля с значением по
 * умолчанию, но OpenAPI помечает их необязательными; `Always` возвращает им обязательность, оставляя
 * тип поля из схемы. Руками здесь описаны только имена, которых в схеме нет.
 */
import type { components } from "./schema.gen";

type Schemas = components["schemas"];

/** Поля, которые сервер присылает всегда (у них есть значение по умолчанию), хотя схема помечает их необязательными. */
type Always<T, K extends keyof T> = Omit<T, K> & Required<Pick<T, K>>;

/** Онлайн-ссылка занятия: «Подключиться», запись лекции, материалы. Подпись задал источник. */
export type EventLink = Schemas["EventLink"];

/** Дополнительный атрибут занятия из источника; подпись общая (без личных значений). Иконку выводит пресет по подписи. */
export type EventAttribute = Schemas["EventAttribute"];

/** `openUrl` — страница интерфейса, где видна запись (`/schedule?begin_date=…`); адрес API (`sourceUrl`) — только провенанс. */
export type Provenance = Always<Schemas["Provenance"], "openUrl">;

interface WithProvenance {
  provenance: Provenance;
}

type Widen<T extends { provenance: Schemas["Provenance"] }> = Omit<T, "provenance"> & WithProvenance;

/** Источник, из которого прочитана пара; первый в `ScheduleEvent.sources` — основной источник расписания вуза. */
export type EventSource = Schemas["EventSource"];

/** Источники расходятся (`kind` — в чём) или похожая пара есть в основном источнике, а в этот час нет (`not_in_primary`). */
export type EventDisagreement = Schemas["EventDisagreement"];

export type ScheduleEvent = Always<
  Widen<Schemas["ScheduleEventOut"]>,
  "kindText" | "address" | "group" | "lessonNumber" | "links" | "attributes" | "sources" | "disagreement"
>;
export type Deadline = Widen<Schemas["DeadlineOut"]>;
export type Course = Widen<Schemas["CourseOut"]>;
export type Grade = Widen<Schemas["GradeOut"]>;
export type Person = Widen<Schemas["PersonOut"]>;
export type NewsItem = Widen<Schemas["NewsOut"]>;

export type Today = Omit<Schemas["TodayOut"], "events" | "deadlines"> & {
  events: ScheduleEvent[];
  deadlines: Deadline[];
};

/** «НИУ ВШЭ»: `shortTitle` — короткое имя вуза для чипов и подписей. */
export type Space = Schemas["SpaceOut"];

/** Вид источника вуза (§3.5). */
export type ConnectionKind = Schemas["ConnectionOut"]["kind"];

/** Фоновая задача источника: что делает агент сейчас; название и тред задачи в ответ не входят. */
export type ConnectionTask = Schemas["ActiveTaskOut"];

/** `entryUrl` — UI-страница входа в данные источника; `threadId` — тред задач источника; `activeTask` — задача сейчас. */
export type Connection = Always<
  Schemas["ConnectionOut"],
  "entryUrl" | "threadId" | "activeTask" | "scheduleRole"
>;

interface Result {
  data?: unknown;
  error?: unknown;
  response: Response;
}

/**
 * Ответ типизированного клиента, у которого поля с умолчанием сделаны обязательными (`Always`, `Widen`).
 * Единственное место приведения типа (`as`): в рантайме оно ничего не проверяет, форму ответа
 * гарантирует сервер (Pydantic присылает поля с умолчанием всегда).
 */
export function asV3<T>(
  request: Promise<Result>,
): Promise<{ data?: T; error?: unknown; response: Response }> {
  return request as Promise<{ data?: T; error?: unknown; response: Response }>;
}

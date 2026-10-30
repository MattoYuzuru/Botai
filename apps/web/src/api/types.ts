import type { components } from "./schema.gen";
import type { MessageOut, ThreadOut } from "./v3";

type Schemas = components["schemas"];

export type Me = Schemas["MeOut"];
export type User = Schemas["UserOut"];
export type Space = Schemas["SpaceOut"];
export type Connection = Schemas["ConnectionOut"];
export type ConnectionStatus = Connection["status"];
export type Section = Schemas["SectionOut"];
export type University = Schemas["UniversityOut"];
export type Today = Schemas["TodayOut"];
export type ScheduleEvent = Schemas["ScheduleEventOut"];
export type Deadline = Schemas["DeadlineOut"];
export type Provenance = Schemas["Provenance"];
/** Тред агент-хаба v3 (`unread`, `attention`, `activeTask`) — см. `v3.ts`. */
export type Thread = ThreadOut;
export type Message = MessageOut;
export type ContextOut = Schemas["ContextOut"];
export type Job = Schemas["JobOut"];

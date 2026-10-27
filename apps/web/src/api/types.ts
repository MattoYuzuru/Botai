import type { components } from "./schema.gen";

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
export type Thread = Schemas["ThreadOut"];
export type Message = Schemas["MessageOut"];
export type ContextOut = Schemas["ContextOut"];
export type Job = Schemas["JobOut"];

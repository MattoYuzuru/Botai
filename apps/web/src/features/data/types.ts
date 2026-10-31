import type { components } from "@/api/schema.gen";

export type { Course, Grade, NewsItem, Person } from "@/api/v3-data";

type Schemas = components["schemas"];

export type DocumentStarted = Schemas["DocumentStarted"];
export type SectionKind = Schemas["SectionOut"]["kind"];
export type EventKind = Schemas["ScheduleEventOut"]["kind"];
export type EventFormat = Schemas["ScheduleEventOut"]["format"];
export type DeadlineState = Schemas["DeadlineOut"]["state"];
export type SecretKind = Schemas["SecretIn"]["kind"];

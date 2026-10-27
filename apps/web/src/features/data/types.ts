import type { components } from "@/api/schema.gen";

type Schemas = components["schemas"];

export type Course = Schemas["CourseOut"];
export type Grade = Schemas["GradeOut"];
export type Person = Schemas["PersonOut"];
export type NewsItem = Schemas["NewsOut"];
export type DocumentRow = Schemas["DocumentOut"];
export type DocumentStarted = Schemas["DocumentStarted"];
export type SectionKind = Schemas["SectionOut"]["kind"];
export type EventKind = Schemas["ScheduleEventOut"]["kind"];
export type EventFormat = Schemas["ScheduleEventOut"]["format"];
export type DeadlineState = Schemas["DeadlineOut"]["state"];
export type SecretKind = Schemas["SecretIn"]["kind"];

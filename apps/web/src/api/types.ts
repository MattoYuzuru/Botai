import type { components } from "./schema.gen";
import type {
  Connection as ConnectionV3,
  Deadline as DeadlineV3,
  Provenance as ProvenanceV3,
  ScheduleEvent as ScheduleEventV3,
  Space as SpaceV3,
  Today as TodayV3,
} from "./v3-data";

export type { ConnectionKind, ConnectionTask, EventAttribute, EventLink } from "./v3-data";

import type { MessageOut, ThreadOut } from "./v3";

type Schemas = components["schemas"];

export type Me = Schemas["MeOut"];
export type User = Schemas["UserOut"];
export type Space = SpaceV3;
export type Connection = ConnectionV3;
export type ConnectionStatus = Connection["status"];
export type Section = Schemas["SectionOut"];
export type University = Schemas["UniversityOut"];
export type Today = TodayV3;
export type ScheduleEvent = ScheduleEventV3;
export type Deadline = DeadlineV3;
export type Provenance = ProvenanceV3;
/** Тред агент-хаба v3 (`unread`, `attention`, `activeTask`) — см. `v3.ts`. */
export type Thread = ThreadOut;
export type Message = MessageOut;
export type ContextOut = Schemas["ContextOut"];
export type Job = Schemas["JobOut"];

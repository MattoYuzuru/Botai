import type { Connection, Provenance, ScheduleEvent, Space } from "@/api/types";
import { type MockConnection, type MockDb, ulid } from "@/mocks/db";

/** Происхождение записи для юнит-тестов: адрес API в `sourceUrl`, страница интерфейса — в `openUrl`. */
export function provenanceFixture(patch: Partial<Provenance> = {}): Provenance {
  return {
    connectionId: "c",
    spaceId: "s",
    sourceUrl: "https://lk.fakeuni.test/api/get-myschedule?begin_date=2026-10-26",
    openUrl: "https://lk.fakeuni.test/schedule?begin_date=2026-10-26",
    fetchedAt: "2026-10-27T14:35:00.000Z",
    stale: false,
    ...patch,
  };
}

/** Пара с полным набором полей контракта v3; всё необязательное пусто. */
export function eventFixture(patch: Partial<ScheduleEvent> = {}): ScheduleEvent {
  return {
    id: "e",
    title: "Алгоритмы и структуры данных",
    kind: "lecture",
    kindText: null,
    format: "offline",
    startsAt: "2026-10-27T08:30:00.000Z",
    endsAt: "2026-10-27T09:50:00.000Z",
    location: null,
    address: null,
    group: null,
    lessonNumber: null,
    teacher: null,
    courseTitle: null,
    links: [],
    attributes: [],
    url: null,
    changed: false,
    conflictsWith: [],
    provenance: provenanceFixture(),
    ...patch,
  };
}

export function spaceFixture(patch: Partial<Space> = {}): Space {
  return {
    id: "s",
    title: "Синтетический университет",
    shortTitle: "СУ",
    aliases: [],
    universityId: null,
    accentHue: 262,
    timeZone: "Europe/Moscow",
    createdAt: "2026-09-01T00:00:00.000Z",
    ...patch,
  };
}

export function connectionFixture(patch: Partial<Connection> = {}): Connection {
  return {
    id: "c",
    spaceId: "s",
    adapter: "recipe",
    kind: "portal",
    origin: "https://lk.fakeuni.test",
    entryUrl: "https://lk.fakeuni.test/schedule",
    threadId: null,
    activeTask: null,
    title: "Личный кабинет",
    status: "active",
    hasPassword: true,
    lastSyncAt: "2026-10-27T14:30:00.000Z",
    nextSyncAt: null,
    errorCode: null,
    errorDetail: null,
    ...patch,
  };
}

/** Адрес ссылки по частям: путь и параметры запроса. Так тесты сверяют `/chat?thread=…` и `/chat?draft=…`. */
export function hrefOf(link: HTMLElement): { pathname: string } & Record<string, string> {
  const url = new URL(link.getAttribute("href") ?? "", "http://localhost");
  return { ...Object.fromEntries(url.searchParams), pathname: url.pathname };
}

/** Добавляет в базу мока источник вуза (с новыми видами `doc`, `site`, `calendar`, которых нет в сгенерированной схеме). */
export function addConnection(db: MockDb, spaceId: string, patch: Partial<Connection>): Connection {
  const connection = connectionFixture({ id: ulid(), spaceId, ...patch });
  db.connections.push(connection as MockConnection);
  return connection;
}

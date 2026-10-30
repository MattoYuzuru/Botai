/**
 * Синтетическая «база» для мок-режима (`VITE_MOCKS=1`) и тестов. Форма данных — по сгенерированным типам
 * OpenAPI, вуз вымышленный (`fakeuni.test`): ничего реального и ничего под конкретный вуз.
 */
import type { components } from "@/api/schema.gen";
import type { Connection, Space } from "@/api/types";
import type { JobVariant } from "./connect-script";
import type { JobSim } from "./job-sim";

type S = components["schemas"];

/**
 * Пространство и подключение мока: сгенерированная форма плюс поля v3, которые отдают экраны данных. Поля
 * необязательны, потому что часть обработчиков (подключение в чате) ещё создаёт записи по прежней форме.
 */
export type MockSpace = S["SpaceOut"] & Partial<Pick<Space, "shortTitle" | "aliases">>;
export type MockConnection = S["ConnectionOut"] &
  Partial<Pick<Connection, "entryUrl" | "threadId" | "activeTask">>;

export type Scenario = "fresh" | "empty" | "connected" | "reauth" | "stale";

export const DEMO = {
  email: "student@example.test",
  // Учебные данные витрины: пароль вымышленного пользователя синтетического стенда.
  password: "correct-horse-battery",
  invite: "BOTAI-DEMO",
  displayName: "Анна",
} as const;

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
let counter = 0;

/** ULID-подобный идентификатор: 26 символов Crockford base32, детерминированный по счётчику. */
export function ulid(): string {
  counter += 1;
  let n = counter;
  let tail = "";
  for (let i = 0; i < 10; i++) {
    tail = CROCKFORD[n % 32] + tail;
    n = Math.floor(n / 32);
  }
  return `01JMKCDB${"0".repeat(8)}${tail}`;
}

export interface MockUser {
  id: string;
  email: string;
  displayName: string;
  password: string;
  isOwner: boolean;
}

export interface MockDb {
  now: () => number;
  users: MockUser[];
  invites: string[];
  /** Идентификатор вошедшего пользователя; сессия живёт в памяти, в браузере — в sessionStorage. */
  sessionUserId: string | null;
  spaces: MockSpace[];
  connections: MockConnection[];
  universities: S["UniversityOut"][];
  /** Задания подключения (живут только в памяти вкладки). */
  sims: Map<string, JobSim>;
  /** Какой сценарий подключения проигрывать (`?job=wrong-password`). */
  jobVariant: JobVariant;
}

const HSE_ORIGIN = "https://lk.fakeuni.test";

export function seedUniversities(): S["UniversityOut"][] {
  return [
    {
      id: "u-hse",
      title: "Высшая школа экономики",
      shortTitle: "ВШЭ",
      accentHue: 262.2,
      origins: [HSE_ORIGIN],
      thirdPartyWarning: null,
    },
    {
      id: "u-cu",
      title: "Центральный университет",
      shortTitle: "ЦУ",
      accentHue: 292,
      origins: ["https://my.fakeuni.test"],
      thirdPartyWarning:
        "Правила вуза ограничивают сторонний доступ к кабинету. Мы подключаемся только на чтение.",
    },
  ];
}

function space(
  title: string,
  shortTitle: string,
  universityId: string | null,
  hue: number,
  createdAt: string,
): MockSpace {
  return {
    id: ulid(),
    title,
    shortTitle,
    aliases: [],
    universityId,
    accentHue: hue,
    timeZone: "Europe/Moscow",
    createdAt,
  };
}

function connection(
  spaceId: string,
  title: string,
  status: MockConnection["status"],
  lastSyncAt: string | null,
  extra: Partial<MockConnection> = {},
): MockConnection {
  const origin = extra.origin ?? HSE_ORIGIN;
  return {
    id: ulid(),
    spaceId,
    adapter: "recipe",
    kind: "portal",
    origin,
    entryUrl: `${origin}/`,
    threadId: null,
    activeTask: null,
    title,
    status,
    hasPassword: true,
    lastSyncAt,
    nextSyncAt: null,
    errorCode: null,
    errorDetail: null,
    ...extra,
  };
}

/** Начальное состояние по сценарию. `now` — «сегодня» мока, чтобы относительные времена были стабильны. */
export function createDb(scenario: Scenario = "fresh", now: () => number = Date.now): MockDb {
  const user: MockUser = {
    id: ulid(),
    email: DEMO.email,
    displayName: DEMO.displayName,
    password: DEMO.password,
    isOwner: false,
  };
  const db: MockDb = {
    now,
    users: [user],
    invites: [DEMO.invite],
    sessionUserId: scenario === "fresh" ? null : user.id,
    spaces: [],
    connections: [],
    universities: seedUniversities(),
    sims: new Map(),
    jobVariant: "basic",
  };
  if (scenario === "fresh" || scenario === "empty") return db;

  const iso = (minutesAgo: number) => new Date(now() - minutesAgo * 60_000).toISOString();
  const hse = space("Высшая школа экономики", "ВШЭ", "u-hse", 262.2, iso(60 * 24 * 40));
  const cu = space("Центральный университет", "ЦУ", "u-cu", 292, iso(60 * 24 * 12));
  db.spaces = [hse, cu];
  db.connections = [
    connection(hse.id, "Личный кабинет", "active", iso(5), { kind: "portal" }),
    connection(hse.id, "Расписание (календарь)", "active", iso(12), {
      kind: "schedule",
      hasPassword: false,
      entryUrl: `${HSE_ORIGIN}/schedule`,
    }),
    connection(
      cu.id,
      "LMS",
      scenario === "reauth" ? "needs_reauth" : scenario === "stale" ? "stale" : "active",
      scenario === "connected" ? iso(30) : iso(60 * 50),
      {
        kind: "lms",
        origin: "https://my.fakeuni.test",
        entryUrl: "https://my.fakeuni.test/learn",
        errorCode: scenario === "reauth" ? "source_auth_required" : null,
      },
    ),
  ];
  return db;
}

export function findUser(db: MockDb, id: string | null): MockUser | undefined {
  return id ? db.users.find((user) => user.id === id) : undefined;
}

/** Сохранение мока между перезагрузками страницы (только в браузере). */
const STORAGE_KEY = "botai.mock.db";

export function persistDb(db: MockDb): void {
  try {
    const { now: _now, sims: _sims, ...rest } = db;
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(rest));
  } catch {
    /* хранилище недоступно */
  }
}

export function restoreDb(now: () => number = Date.now): MockDb | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    return raw ? { ...(JSON.parse(raw) as Omit<MockDb, "now" | "sims">), now, sims: new Map() } : null;
  } catch {
    return null;
  }
}

/**
 * Синтетическая «база» для мок-режима (`VITE_MOCKS=1`) и тестов. Форма данных — по сгенерированным типам
 * OpenAPI, вуз вымышленный (`fakeuni.test`): ничего реального и ничего под конкретный вуз.
 */
import type { components } from "@/api/schema.gen";
import type { Connection, Space } from "@/api/types";
import type { DeadlineUserState, MemoryItem, StatusRule } from "@/api/v3-data";
import type { JobVariant } from "./connect-script";
import type { JobSim } from "./job-sim";
import { seedPanelWorld } from "./panels-seed";

type S = components["schemas"];

/**
 * Пространство и подключение мока: сгенерированная форма плюс поля v3, которые отдают экраны данных. Поля
 * необязательны, потому что часть обработчиков (подключение в чате) ещё создаёт записи по прежней форме.
 */
/** Пространство мока: у v4-API пространство всегда вуз (`universityId: string`). */
export type MockSpace = S["SpaceOut"] & Partial<Pick<Space, "shortTitle" | "aliases">>;
export type MockConnection = S["ConnectionOut"] &
  Partial<Pick<Connection, "entryUrl" | "threadId" | "activeTask" | "scheduleRole">>;
/** Источник v4: сгенерированная форма `SourceOut`, как её отдаёт `/api/v1/sources`. */
export type MockSource = S["SourceOut"];
/** Строка политики автономии (`PolicyOut`) и запись журнала действий (`ActionOut`). */
export type MockPolicy = S["PolicyOut"];
export type MockAction = S["ActionOut"];
/** Запуск источника (`SourceRunOut`): задание, его итог, ошибка и ссылки на тред/трассу. */
export type MockSourceRun = S["SourceRunOut"];
/** Панель «Учёбы» без данных и вуза: данные строит обработчик, вуз берётся из `spaceId`. */
export type MockPanel = Omit<S["PanelOut"], "data" | "space"> & { spaceId: string };

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
  /**
   * Источники v4 — отдельная модель рядом с подключениями: старые экраны читают провенанс из
   * `connections`, «Учёба» и карточка источника — из `/api/v1/sources`. Сиды держат обе формы
   * согласованными по смыслу, а действия источника меняют только его запись.
   */
  sources: MockSource[];
  /** Playbook источника и его ревизии: `null` в `playbookId` — playbook ещё не создан. */
  playbooks: Record<string, S["PlaybookOut"]>;
  playbookVersions: Record<string, S["PlaybookVersionOut"][]>;
  /** Артефакты прочитанного: страницы, JSON, документы. */
  artifacts: S["ArtifactOut"][];
  /** Порядок источников по виду данных (`${spaceId}:${kind}`) — настройка «основной источник». */
  sourcePriorities: Record<string, string[]>;
  /** Явные строки политики автономии владельца; без строки действует дефолт (docs/v4/19-actions.md). */
  actionPolicies: MockPolicy[];
  /** Журнал действий: предложения, решения и итоги. Тела запросов сервер не хранит. */
  actionsLog: MockAction[];
  /** История запусков источников: задания connect/sync/repair и их итоги. */
  sourceRuns: MockSourceRun[];
  /** Панели «Успеваемости» (D-039): конфиги; данные собираются при запросе. */
  panels: MockPanel[];
  universities: S["UniversityOut"][];
  /** Отметки студента на заданиях (`PATCH /deadlines/{id}`, D-045): сильнее статуса источника. */
  deadlineMarks: Record<string, Exclude<DeadlineUserState, null>>;
  /** Правила статусов студента (`/status-rules`). */
  statusRules: StatusRule[];
  /** Память агента (`/me/memory`): заметки и правила студента. */
  memory: MemoryItem[];
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
      aliases: ["Вышка"],
      accentHue: 262.2,
      origins: [HSE_ORIGIN],
      thirdPartyWarning: null,
    },
    {
      id: "u-cu",
      title: "Центральный университет",
      shortTitle: "ЦУ",
      aliases: [],
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
  universityId: string,
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
    sources: [],
    playbooks: {},
    playbookVersions: {},
    artifacts: [],
    sourcePriorities: {},
    actionPolicies: [],
    actionsLog: [],
    sourceRuns: [],
    panels: [],
    deadlineMarks: {},
    statusRules: [],
    memory: [],
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
  seedSourceWorld(db, scenario, iso);
  seedActionWorld(db, iso);
  seedPanelWorld(db, iso);
  seedCorrections(db, iso);
  return db;
}

/**
 * Поправки студента (D-045): правило статуса «Решение прикреплено» → сдано у кабинета ВШЭ, отметка «сдал» на
 * задании без статуса в источнике, правила и заметки памяти агента. Тексты синтетические.
 */
function seedCorrections(db: MockDb, iso: (minutesAgo: number) => string): void {
  db.statusRules = [
    {
      id: "rule-hse-attached",
      sourceId: "src-hse-portal",
      sourceTitle: "Личный кабинет",
      label: "Решение прикреплено",
      state: "submitted",
      createdAt: iso(60 * 24 * 2),
    },
  ];
  // «Конспект лекции 5» (ЦУ) — источник статуса не знает, студент отметил «сдал».
  db.deadlineMarks = { "dl-12": "submitted" };
  db.memory = [
    {
      id: "mem-rule-1",
      kind: "rule",
      note: "Про оценки сначала смотри LMS ЦУ, потом личный кабинет ВШЭ",
      createdAt: iso(60 * 24 * 3),
    },
    {
      id: "mem-rule-2",
      kind: "rule",
      note: "Сроки заданий называй по московскому времени",
      createdAt: iso(60 * 24 * 6),
    },
    {
      id: "mem-note-1",
      kind: "note",
      note: "Учится на 3 курсе, группа ПМ-3",
      createdAt: iso(60 * 24 * 10),
    },
    {
      id: "mem-note-2",
      kind: "note",
      note: "Курсовую пишет по теории вероятностей",
      createdAt: iso(60 * 24 * 14),
    },
  ];
}

/**
 * Источники v4, их playbook'и, ревизии, артефакты и порядок для склейки. Сиды — вымышленный вуз
 * `fakeuni.test`; состояния совпадают по смыслу с подключениями, чтобы «Учёба» и старые экраны не
 * рассказывали разное об одном вузе.
 */
function seedSourceWorld(db: MockDb, scenario: Scenario, iso: (minutesAgo: number) => string): void {
  const hse = db.spaces[0] as MockSpace;
  const cu = db.spaces[1] as MockSpace;
  const lmsStatus = scenario === "reauth" ? "needs_reauth" : scenario === "stale" ? "stale" : "active";
  const lmsSyncedAt = scenario === "connected" ? iso(30) : iso(60 * 50);

  db.sources = [
    {
      id: "src-hse-portal",
      spaceId: hse.id,
      universityId: "u-hse",
      title: "Личный кабинет",
      entryUrl: `${HSE_ORIGIN}/`,
      kinds: ["portal", "schedule", "deadlines"],
      status: "active",
      playbookId: "pb-hse-portal",
      config: {},
      authRef: "secret:hse-portal",
      priority: 0,
      trust: "high",
      lastSyncAt: iso(5),
      nextSyncAt: iso(-45),
      lastError: null,
      disabledFrom: null,
      threadId: "01JMKCDB00000000000000THR",
      createdAt: iso(60 * 24 * 40),
      updatedAt: iso(5),
    },
    {
      id: "src-hse-calendar",
      spaceId: hse.id,
      universityId: "u-hse",
      title: "Расписание (календарь)",
      entryUrl: `${HSE_ORIGIN}/schedule`,
      kinds: ["calendar", "schedule"],
      status: "active",
      playbookId: "pb-hse-calendar",
      config: {},
      authRef: null,
      priority: 1,
      trust: "normal",
      lastSyncAt: iso(12),
      nextSyncAt: iso(-120),
      lastError: null,
      disabledFrom: null,
      createdAt: iso(60 * 24 * 30),
      updatedAt: iso(12),
    },
    {
      id: "src-cu-lms",
      spaceId: cu.id,
      universityId: "u-cu",
      title: "LMS",
      entryUrl: "https://my.fakeuni.test/learn",
      kinds: ["lms", "deadlines", "grades"],
      status: lmsStatus,
      playbookId: "pb-cu-lms",
      config: {},
      authRef: "secret:cu-lms",
      priority: 0,
      trust: "normal",
      lastSyncAt: lmsSyncedAt,
      nextSyncAt: iso(-90),
      lastError:
        scenario === "reauth"
          ? {
              code: "source_auth_required",
              detail: "Нужно снова войти в личный кабинет: сессия источника закончилась.",
            }
          : null,
      disabledFrom: null,
      createdAt: iso(60 * 24 * 12),
      updatedAt: lmsSyncedAt,
    },
  ];

  db.playbooks = {
    "pb-hse-portal": {
      id: "pb-hse-portal",
      universityId: "u-hse",
      ownerId: null,
      title: "ЛК ВШЭ: расписание и задания",
      language: "declarative",
      currentVersion: 2,
      health: {
        ok: true,
        last_run: "canary",
        checked_at: iso(5),
        counts: { schedule: 12, deadlines: 4 },
        error_count: 0,
      },
      provenance: { by: "agent", learned_at: iso(60 * 24 * 30), model: "mock" },
      createdAt: iso(60 * 24 * 30),
    },
    "pb-hse-calendar": {
      id: "pb-hse-calendar",
      universityId: "u-hse",
      ownerId: null,
      title: "Календарь ВШЭ по ссылке",
      language: "recipe",
      currentVersion: 1,
      health: { ok: true, last_run: "sync", checked_at: iso(12), counts: { schedule: 8 }, error_count: 0 },
      provenance: { by: "agent", learned_at: iso(60 * 24 * 27), model: "mock" },
      createdAt: iso(60 * 24 * 27),
    },
    "pb-cu-lms": {
      id: "pb-cu-lms",
      universityId: "u-cu",
      ownerId: null,
      title: "LMS ЦУ: курсы и сроки",
      language: "python",
      currentVersion: 1,
      health: { ok: lmsStatus === "active", last_run: "sync", checked_at: lmsSyncedAt, error_count: 0 },
      provenance: { by: "agent", learned_at: iso(60 * 24 * 12), model: "mock" },
      createdAt: iso(60 * 24 * 12),
    },
  };
  const version = (
    playbookId: string,
    number: number,
    active: boolean,
    createdAt: string,
  ): S["PlaybookVersionOut"] => ({
    id: `pbv-${playbookId}-${number}`,
    playbookId,
    version: number,
    spec: { entry: "schedule", kinds: ["schedule"] },
    code: null,
    tests: [{ name: "расписание недели", ok: true }],
    canary: { ok: true, checked_at: createdAt },
    active,
    checksum: `sha256:${playbookId}-${number}`,
    createdByRun: `run-${playbookId}-${number}`,
    createdAt,
  });
  db.playbookVersions = {
    "pb-hse-portal": [
      version("pb-hse-portal", 1, false, iso(60 * 24 * 30)),
      version("pb-hse-portal", 2, true, iso(60 * 24 * 2)),
    ],
    "pb-hse-calendar": [version("pb-hse-calendar", 1, true, iso(60 * 24 * 27))],
    "pb-cu-lms": [version("pb-cu-lms", 1, true, iso(60 * 24 * 12))],
  };

  db.artifacts = [
    {
      id: "art-hse-schedule",
      sourceId: "src-hse-portal",
      kind: "page",
      url: `${HSE_ORIGIN}/schedule`,
      sha256: "a".repeat(64),
      contentType: "text/html",
      size: 18_420,
      metadata: { title: "Расписание", group: "ПМ-3" },
      capturedAt: iso(5),
      runId: "run-hse-portal-2",
      step: 0,
    },
    {
      id: "art-hse-program",
      sourceId: "src-hse-portal",
      kind: "doc",
      url: `${HSE_ORIGIN}/docs/programma-analiz.pdf`,
      sha256: "b".repeat(64),
      contentType: "application/pdf",
      size: 240_512,
      metadata: { title: "Программа курса: математический анализ.pdf" },
      capturedAt: iso(5),
      runId: null,
      step: null,
    },
    {
      id: "art-hse-tasks",
      sourceId: "src-hse-portal",
      kind: "json",
      url: `${HSE_ORIGIN}/api/tasks`,
      sha256: "c".repeat(64),
      contentType: "application/json",
      size: 6_204,
      metadata: { title: "Сроки заданий" },
      capturedAt: iso(5),
      runId: "run-hse-portal-2",
      step: 1,
    },
    {
      id: "art-calendar-ics",
      sourceId: "src-hse-calendar",
      kind: "file",
      url: `${HSE_ORIGIN}/schedule/export.ics`,
      sha256: "d".repeat(64),
      contentType: "text/calendar",
      size: 3_102,
      metadata: { title: "Календарь вуза" },
      capturedAt: iso(12),
      runId: null,
      step: null,
    },
    {
      id: "art-cu-courses",
      sourceId: "src-cu-lms",
      kind: "json",
      url: "https://my.fakeuni.test/api/courses",
      sha256: "e".repeat(64),
      contentType: "application/json",
      size: 9_820,
      metadata: { title: "Курсы LMS" },
      capturedAt: lmsSyncedAt,
      runId: null,
      step: null,
    },
  ];

  // Порядок для склейки расписания уже выбран владельцем: кабинет — основной, календарь — дополнительный.
  db.sourcePriorities = { [`${hse.id}:schedule`]: ["src-hse-portal", "src-hse-calendar"] };

  const run = (
    id: string,
    sourceId: string,
    kind: string,
    status: string,
    startedAt: string,
    extra: Partial<MockSourceRun> = {},
  ): MockSourceRun => ({
    id,
    sourceId,
    kind,
    status,
    ok: status === "done" ? true : status === "failed" ? false : null,
    startedAt,
    finishedAt: status === "running" || status === "queued" ? null : startedAt,
    counts: {},
    errorCode: null,
    errorDetail: null,
    threadId: null,
    runId: null,
    ...extra,
  });
  db.sourceRuns = [
    run("job-portal-sync-2", "src-hse-portal", "sync", "done", iso(6), {
      counts: { schedule: 12, deadlines: 4 },
      threadId: "01JMKCDB00000000000000THR",
      runId: "run-answer",
    }),
    run("job-portal-connect", "src-hse-portal", "connect", "done", iso(60 * 24 * 30), {
      counts: { schedule: 12, deadlines: 4 },
      threadId: "01JMKCDB00000000000000THR",
    }),
    run("job-portal-repair", "src-hse-portal", "repair", "failed", iso(60 * 24), {
      errorCode: "recipe_drift",
      errorDetail: "Сайт вуза изменил разметку — нужна починка описания.",
    }),
    run("job-calendar-sync", "src-hse-calendar", "sync", "done", iso(12), {
      counts: { schedule: 8 },
    }),
    run("job-cu-sync", "src-cu-lms", "sync", "failed", lmsSyncedAt, {
      errorCode: "source_auth_required",
      errorDetail: "Сессия источника закончилась.",
    }),
  ];
}

/**
 * Журнал действий мок-стенда: спрошенное, автоматическое, запрещённое и неудавшееся действия.
 * Тела запросов и значения полей — синтетические, уже «замаскированные», как их отдаёт сервер.
 */
function seedActionWorld(db: MockDb, iso: (minutesAgo: number) => string): void {
  const action = (
    id: number,
    decision: string,
    mode: string,
    actionClass: string,
    intent: string,
    createdAt: string,
    extra: Partial<MockAction> = {},
  ): MockAction => ({
    id,
    decision,
    mode,
    actionClass,
    scopeKind: "source",
    scopeId: "src-hse-portal",
    intent,
    proposal: {
      transport: "http",
      intent,
      source_id: "src-hse-portal",
      space_id: db.spaces[0]?.id ?? null,
      method: "POST",
      url: "https://lk.fakeuni.test/api/requests",
      body_kind: "form",
      fields: [
        { name: "course", value: "Алгоритмы и структуры данных" },
        { name: "comment", value: "Запись через BotAI" },
      ],
      header_names: ["content-type"],
      cookie_names: ["sessionid"],
    },
    request: null,
    result: null,
    rollback: null,
    idempotencyKey: `run:demo:sha256-${id}`,
    runId: "run-answer",
    jobId: "01JMKCDB00000000000000JOB",
    sourceId: "src-hse-portal",
    sourceTitle: "Личный кабинет",
    createdAt,
    decidedAt: null,
    executedAt: null,
    ...extra,
  });

  db.actionsLog = [
    action(1, "asked", "ask", "write_soft", "Записаться на пересдачу по алгоритмам", iso(5), {
      rollback: { hint: "Отменить запись можно в личном кабинете вуза." },
    }),
    action(2, "executed", "auto", "write_soft", "Отправить заявку на консультацию", iso(80), {
      request: {
        method: "POST",
        url: "https://lk.fakeuni.test/api/requests",
        body_kind: "form",
        body_bytes: 128,
        body_sha256: "f".repeat(64),
      },
      result: { status: 200, summary: "Заявка принята личным кабинетом." },
      decidedAt: iso(80),
      executedAt: iso(79),
    }),
    action(3, "denied", "ask", "money", "Оплатить доступ к дополнительному курсу", iso(150), {
      decidedAt: iso(149),
    }),
    action(4, "failed", "auto", "write_destructive", "Отозвать заявление на пересдачу", iso(300), {
      request: {
        method: "DELETE",
        url: "https://lk.fakeuni.test/api/requests/42",
        body_kind: "json",
        body_bytes: 0,
        body_sha256: "e".repeat(64),
      },
      result: { status: 500, summary: "Сайт вуза ответил ошибкой." },
      decidedAt: iso(300),
      executedAt: iso(299),
    }),
    action(5, "never", "never", "money", "Купить полный доступ к курсу", iso(500), {
      scopeKind: "global",
      scopeId: null,
      sourceId: null,
      sourceTitle: null,
      runId: null,
      jobId: null,
    }),
  ];
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
    if (!raw) return null;
    // Поля, добавленные позже, у сохранённой вкладки могут отсутствовать: мок не должен падать на старом снимке.
    const parsed = JSON.parse(raw) as Omit<MockDb, "now" | "sims">;
    return {
      ...parsed,
      sources: parsed.sources ?? [],
      actionPolicies: parsed.actionPolicies ?? [],
      actionsLog: parsed.actionsLog ?? [],
      sourceRuns: parsed.sourceRuns ?? [],
      panels: parsed.panels ?? [],
      deadlineMarks: parsed.deadlineMarks ?? {},
      statusRules: parsed.statusRules ?? [],
      memory: parsed.memory ?? [],
      playbooks: parsed.playbooks ?? {},
      playbookVersions: parsed.playbookVersions ?? {},
      artifacts: parsed.artifacts ?? [],
      sourcePriorities: parsed.sourcePriorities ?? {},
      now,
      sims: new Map(),
    };
  } catch {
    return null;
  }
}

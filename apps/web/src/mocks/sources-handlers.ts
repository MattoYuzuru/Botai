/**
 * MSW-обработчики источников v4: `/api/v1/sources*`, playbook, артефакты и порядок источников.
 *
 * Форма ответов — по OpenAPI (`SourceOut`, `RunReportOut`, `PlaybookOut`, …), поэтому «Учёба» и e2e
 * идут тем же путём, что и настоящий v4-API, а не через fallback на прежние подключения. Действия
 * меняют сид: sync/repair делают источник свежим, disable/enable переключают его, priority —
 * порядок склейки. Секретов в ответах нет, как и у сервера.
 */
import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import { findUser, type MockDb, type MockSource, persistDb } from "./db";

type S = components["schemas"];

export interface SourcesHandlerOptions {
  latency?: number;
  persist?: boolean;
}

function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

/** Отчёт прогона в форме сервера: честные счётчики и артефакты, без выдуманных процентов. */
function runReport(db: MockDb, source: MockSource, run: "SYNC" | "REPAIR"): S["RunReportOut"] {
  const startedAt = new Date(db.now() - 1200).toISOString();
  const finishedAt = new Date(db.now()).toISOString();
  const dataKinds = source.kinds.filter((kind) =>
    ["schedule", "deadlines", "assignments", "courses", "grades", "documents", "news"].includes(kind),
  );
  const reads: S["ReadOutcomeOut"][] = [
    {
      name: "основное чтение",
      url: source.entryUrl,
      format: "json",
      ok: true,
      status: 200,
      itemsSeen: 12,
      itemsValid: 12,
      itemsInvalid: 0,
      artifactId: db.artifacts.find((artifact) => artifact.sourceId === source.id)?.id ?? null,
      error: null,
    },
  ];
  return {
    sourceId: source.id,
    run,
    version: source.playbookId ? 2 : null,
    ok: true,
    sourceStatus: "active",
    startedAt,
    finishedAt,
    reads,
    counts: Object.fromEntries(dataKinds.map((kind) => [kind, 12])),
    records: [],
    recordsTruncated: false,
    errors: [],
    artifactIds: db.artifacts
      .filter((artifact) => artifact.sourceId === source.id)
      .slice(0, 2)
      .map((artifact) => artifact.id),
    cursor: null,
    notes: [],
    persisted: true,
    // Новые поля RunReportOut (запись в домен и network-first); мок отдаёт честно пустые
    // значения, а не выдуманные счётчики.
    domain: {},
    discoveredEndpoints: [],
  };
}

/**
 * Состояние источников из адреса (только стенд/e2e): `?sources=none` — вузы без источников,
 * `?sources=fail` — честная ошибка загрузки. Как `?data=` у экранов данных.
 */
function readSourcesVariant(): "none" | "fail" | null {
  try {
    const value = new URLSearchParams(window.location.search).get("sources");
    return value === "none" || value === "fail" ? value : null;
  } catch {
    return null;
  }
}

export function createSourcesHandlers(db: MockDb, options: SourcesHandlerOptions = {}) {
  const { latency = 0, persist = false } = options;
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const save = () => {
    if (persist) persistDb(db);
  };
  const signedIn = () => Boolean(findUser(db, db.sessionUserId));
  const unauthorized = () => problem(401, "unauthorized", "Нужно войти в BotAI");
  const found = (id: string) => db.sources.find((source) => source.id === id);
  const nowIso = () => new Date(db.now()).toISOString();
  const touch = (source: MockSource) => {
    source.updatedAt = nowIso();
    save();
  };

  return [
    http.get("*/api/v1/sources", async ({ request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const variant = readSourcesVariant();
      if (variant === "fail") return problem(500, "internal_error", "Источники временно недоступны");
      if (variant === "none") return HttpResponse.json([]);
      const url = new URL(request.url);
      const spaceId = url.searchParams.get("space_id");
      const status = url.searchParams.get("status");
      const kind = url.searchParams.get("kind");
      const rows = db.sources
        .filter((source) => (spaceId ? source.spaceId === spaceId : true))
        .filter((source) => (status ? source.status === status : true))
        .filter((source) => (kind ? source.kinds.includes(kind) : true));
      return HttpResponse.json(rows);
    }),

    http.get("*/api/v1/sources/:sourceId", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      return source ? HttpResponse.json(source) : problem(404, "not_found", "Источник не найден");
    }),

    http.patch("*/api/v1/sources/:sourceId", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      if (!source) return problem(404, "not_found", "Источник не найден");
      const body = (await request.json()) as Partial<S["SourcePatch"]>;
      for (const key of ["title", "entryUrl", "kinds", "config", "trust", "priority"] as const) {
        if (key in body && body[key] !== undefined) Object.assign(source, { [key]: body[key] });
      }
      touch(source);
      return HttpResponse.json(source);
    }),

    http.post("*/api/v1/sources/:sourceId/sync", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      if (!source) return problem(404, "not_found", "Источник не найден");
      if (source.status === "disabled")
        return problem(409, "conflict", "Источник выключен: сначала включите его");
      if (source.status === "needs_reauth" || source.status === "needs_link")
        return problem(409, "source_auth_required", "Источнику нужен вход или новая ссылка");
      const report = runReport(db, source, "SYNC");
      source.status = "active";
      source.lastError = null;
      source.lastSyncAt = nowIso();
      touch(source);
      return HttpResponse.json(report);
    }),

    http.post("*/api/v1/sources/:sourceId/repair", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      if (!source) return problem(404, "not_found", "Источник не найден");
      if (source.status === "disabled")
        return problem(409, "conflict", "Источник выключен: сначала включите его");
      const report = runReport(db, source, "REPAIR");
      source.status = "active";
      source.lastError = null;
      source.lastSyncAt = nowIso();
      touch(source);
      return HttpResponse.json(report);
    }),

    http.post("*/api/v1/sources/:sourceId/disable", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      if (!source) return problem(404, "not_found", "Источник не найден");
      if (source.status !== "disabled") {
        source.disabledFrom = source.status;
        source.status = "disabled";
        touch(source);
      }
      return HttpResponse.json(source);
    }),

    http.post("*/api/v1/sources/:sourceId/enable", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      if (!source) return problem(404, "not_found", "Источник не найден");
      if (source.status === "disabled") {
        source.status = source.disabledFrom ?? "active";
        source.disabledFrom = null;
        touch(source);
      }
      return HttpResponse.json(source);
    }),

    http.get("*/api/v1/sources/:sourceId/playbook", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      if (!source) return problem(404, "not_found", "Источник не найден");
      const playbook = source.playbookId ? db.playbooks[source.playbookId] : undefined;
      return playbook
        ? HttpResponse.json(playbook)
        : problem(404, "not_found", "У источника ещё нет playbook");
    }),

    http.get("*/api/v1/sources/:sourceId/playbook/versions", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      if (!source) return problem(404, "not_found", "Источник не найден");
      return HttpResponse.json(source.playbookId ? (db.playbookVersions[source.playbookId] ?? []) : []);
    }),

    http.get("*/api/v1/sources/:sourceId/artifacts", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const source = found(String(params.sourceId));
      if (!source) return problem(404, "not_found", "Источник не найден");
      const limit = Number(new URL(request.url).searchParams.get("limit") ?? 20);
      const rows = db.artifacts
        .filter((artifact) => artifact.sourceId === source.id)
        .slice()
        .sort((a, b) => b.capturedAt.localeCompare(a.capturedAt))
        .slice(0, Number.isFinite(limit) ? limit : 20);
      return HttpResponse.json(rows);
    }),

    http.get("*/api/v1/spaces/:spaceId/priorities/:kind", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const spaceId = String(params.spaceId);
      if (!db.spaces.some((space) => space.id === spaceId))
        return problem(404, "not_found", "Пространство не найдено");
      const kind = String(params.kind);
      return HttpResponse.json({
        spaceId,
        kind,
        sourceIds: db.sourcePriorities[`${spaceId}:${kind}`] ?? [],
      } satisfies S["PriorityOut"]);
    }),

    http.put("*/api/v1/spaces/:spaceId/priorities/:kind", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const spaceId = String(params.spaceId);
      if (!db.spaces.some((space) => space.id === spaceId))
        return problem(404, "not_found", "Пространство не найдено");
      const kind = String(params.kind);
      const body = (await request.json()) as S["PriorityIn"];
      const ids = [...new Set(body.sourceIds)];
      for (const id of ids) {
        const source = found(id);
        if (!source || source.spaceId !== spaceId)
          return problem(404, "not_found", "Источник не относится к этому вузу");
        if (source.status === "disabled")
          return problem(409, "conflict", "Отключённый источник нельзя сделать основным");
      }
      db.sourcePriorities[`${spaceId}:${kind}`] = ids;
      save();
      return HttpResponse.json({ spaceId, kind, sourceIds: ids } satisfies S["PriorityOut"]);
    }),
  ];
}

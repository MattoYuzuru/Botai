/**
 * MSW-обработчики автономии и журнала действий (docs/v4/19-actions.md §4): политики
 * `auto/ask/never` и их эффективное разрешение, журнал и решение по `ask`.
 *
 * Правило «источник > вуз > сайт > глобально» повторяет сервер (`actions/policy.py`), а дефолты —
 * задокументированные: чтение сразу, изменения/деньги/вход спрашивают. Тела запросов журнал не
 * хранит — только метод, адрес, вид и размер тела; значения полей в сиде уже замаскированы.
 */
import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import { findUser, type MockAction, type MockDb, type MockPolicy, persistDb } from "./db";

type S = components["schemas"];

type ActionClass = "read" | "write_soft" | "write_destructive" | "money" | "auth";
type ActionMode = "auto" | "ask" | "never";
type ScopeKind = "global" | "site" | "space" | "source";
type Variant = "empty" | "fail";

const CLASSES: readonly ActionClass[] = ["read", "write_soft", "write_destructive", "money", "auth"];
const DEFAULT_MODES: Record<ActionClass, ActionMode> = {
  read: "auto",
  write_soft: "ask",
  write_destructive: "ask",
  money: "ask",
  auth: "ask",
};
const SCOPE_RANK: Record<ScopeKind, number> = { global: 0, site: 1, space: 2, source: 3 };

export interface ActionHandlerOptions {
  latency?: number;
}

function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

function readVariant(): Variant | null {
  try {
    const value = new URLSearchParams(window.location.search).get("actions");
    return value === "empty" || value === "fail" ? value : null;
  } catch {
    return null;
  }
}

function siteOf(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.hostname.toLowerCase() : null;
  } catch {
    return null;
  }
}

export function createActionHandlers(db: MockDb, options: ActionHandlerOptions = {}) {
  const { latency = 0 } = options;
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const save = () => persistDb(db);
  const signedIn = () => Boolean(findUser(db, db.sessionUserId));
  const unauthorized = () => problem(401, "unauthorized", "Нужно войти в BotAI");
  const nowIso = () => new Date(db.now()).toISOString();
  const nextPolicyId = () => {
    const max = db.actionPolicies.reduce((current, policy) => {
      const match = /^pol-(\d+)$/.exec(policy.id);
      return match ? Math.max(current, Number(match[1])) : current;
    }, 0);
    return `pol-${max + 1}`;
  };

  /** Эффективный режим каждого класса: самая узкая подходящая строка или дефолт сервера. */
  const effective = (input: { url?: string | null; sourceId?: string | null; spaceId?: string | null }) => {
    const site = siteOf(input.url ?? null);
    return CLASSES.map((actionClass): S["EffectivePolicyOut"] => {
      const matches = db.actionPolicies.filter((policy) => {
        if (policy.actionClass !== actionClass) return false;
        switch (policy.scopeKind) {
          case "global":
            return true;
          case "site":
            return site !== null && policy.scopeId === site;
          case "space":
            return Boolean(input.spaceId) && policy.scopeId === input.spaceId;
          case "source":
            return Boolean(input.sourceId) && policy.scopeId === input.sourceId;
          default:
            return false;
        }
      });
      const chosen = matches.reduce<MockPolicy | null>(
        (best, row) =>
          best === null ||
          (SCOPE_RANK[row.scopeKind as ScopeKind] ?? 0) >= (SCOPE_RANK[best.scopeKind as ScopeKind] ?? 0)
            ? row
            : best,
        null,
      );
      if (!chosen) {
        return {
          actionClass,
          mode: DEFAULT_MODES[actionClass],
          scopeKind: "global",
          scopeId: null,
          limits: {},
          escalated: null,
        };
      }
      return {
        actionClass,
        mode: chosen.mode,
        scopeKind: chosen.scopeKind,
        scopeId: chosen.scopeId,
        limits: chosen.limits as Record<string, number>,
        escalated: null,
      };
    });
  };

  return [
    http.get("*/api/v1/action-policies", async () => {
      await wait();
      if (!signedIn()) return unauthorized();
      return HttpResponse.json(db.actionPolicies);
    }),

    http.put("*/api/v1/action-policies", async ({ request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const body = (await request.json()) as S["PolicyIn"];
      if (body.scopeKind === "global" && body.scopeId) {
        return problem(422, "validation", "Глобальная политика не имеет scope_id");
      }
      if (body.scopeKind !== "global" && !body.scopeId) {
        return problem(422, "validation", "Для этой области нужен scope_id");
      }
      const existing = db.actionPolicies.find(
        (policy) =>
          policy.scopeKind === body.scopeKind &&
          policy.actionClass === body.actionClass &&
          (policy.scopeId ?? null) === (body.scopeId ?? null),
      );
      if (existing) {
        existing.mode = body.mode;
        existing.limits = body.limits ?? {};
        existing.note = body.note ?? null;
        existing.updatedAt = nowIso();
        save();
        return HttpResponse.json(existing);
      }
      const created: MockPolicy = {
        id: nextPolicyId(),
        scopeKind: body.scopeKind,
        scopeId: body.scopeId ?? null,
        actionClass: body.actionClass,
        mode: body.mode,
        limits: body.limits ?? {},
        note: body.note ?? null,
        updatedAt: nowIso(),
      };
      db.actionPolicies.push(created);
      save();
      return HttpResponse.json(created);
    }),

    http.get("*/api/v1/action-policies/effective", async ({ request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const url = new URL(request.url);
      return HttpResponse.json(
        effective({
          url: url.searchParams.get("url"),
          sourceId: url.searchParams.get("source_id"),
          spaceId: url.searchParams.get("space_id"),
        }),
      );
    }),

    http.delete("*/api/v1/action-policies/:policyId", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const id = String(params.policyId);
      const before = db.actionPolicies.length;
      db.actionPolicies = db.actionPolicies.filter((policy) => policy.id !== id);
      if (db.actionPolicies.length === before) return problem(404, "not_found", "Политика не найдена");
      save();
      return new HttpResponse(null, { status: 204 });
    }),

    http.get("*/api/v1/actions", async ({ request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const variant = readVariant();
      if (variant === "fail") return problem(500, "internal_error", "Журнал временно недоступен");
      if (variant === "empty") return HttpResponse.json([]);
      const url = new URL(request.url);
      const sourceId = url.searchParams.get("source_id");
      const actionClass = url.searchParams.get("action_class");
      const decision = url.searchParams.get("decision");
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const size = Number.isFinite(limit) ? Math.max(1, Math.min(200, Math.trunc(limit))) : 50;
      const rows = db.actionsLog
        .filter((action) => (sourceId ? action.sourceId === sourceId : true))
        .filter((action) => (actionClass ? action.actionClass === actionClass : true))
        .filter((action) => (decision ? action.decision === decision : true))
        .slice()
        .sort((left, right) => right.createdAt.localeCompare(left.createdAt))
        .slice(0, size);
      return HttpResponse.json(rows);
    }),

    http.get("*/api/v1/actions/:actionId", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const action = db.actionsLog.find((row) => row.id === Number(params.actionId));
      return action ? HttpResponse.json(action) : problem(404, "not_found", "Действие не найдено");
    }),

    http.post("*/api/v1/actions/:actionId/decision", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const action = db.actionsLog.find((row) => row.id === Number(params.actionId));
      if (!action) return problem(404, "not_found", "Действие не найдено");
      const body = (await request.json()) as S["DecisionIn"];
      if (action.decision !== "asked") {
        return problem(409, "conflict", "Действие уже решено или не ждало решения");
      }
      action.decision = body.decision === "approve" ? "approved" : "denied";
      action.decidedAt = nowIso();
      save();
      const updated: MockAction = action;
      return HttpResponse.json(updated);
    }),
  ];
}

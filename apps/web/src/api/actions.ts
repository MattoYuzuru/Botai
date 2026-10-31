import { queryOptions } from "@tanstack/react-query";
import { api, call } from "./client";
import type { components } from "./schema.gen";

/**
 * Политика автономии и журнал действий (docs/v4/19-actions.md, D-006): типы — из сгенерированного
 * OpenAPI. Решение `ask` записывается здесь, а выполняет действие ход агента, который его предложил:
 * журнал намеренно не хранит тело запроса, поэтому клиент ничего не отправляет сам.
 */
type Schemas = components["schemas"];

export type ActionPolicy = Schemas["PolicyOut"];
export type ActionPolicyIn = Schemas["PolicyIn"];
export type EffectivePolicy = Schemas["EffectivePolicyOut"];
export type ActionJournalEntry = Schemas["ActionOut"];
export type ActionDecision = NonNullable<Schemas["DecisionIn"]["decision"]>;
export type ActionJournalDecision = ActionJournalEntry["decision"];

export interface JournalFilters {
  sourceId?: string;
  actionClass?: string;
  decision?: string;
  limit?: number;
}

const path = (actionId: number) => ({ path: { action_id: actionId } });

export const actionKeys = {
  all: ["actions"] as const,
  policies: ["actions", "policies"] as const,
  effective: (sourceId?: string, spaceId?: string, url?: string) =>
    ["actions", "effective", sourceId ?? "", spaceId ?? "", url ?? ""] as const,
  journalAll: ["actions", "journal"] as const,
  journal: (filters: JournalFilters) =>
    [
      "actions",
      "journal",
      filters.sourceId ?? "",
      filters.actionClass ?? "",
      filters.decision ?? "",
      filters.limit ?? 50,
    ] as const,
};

/** Все явные строки политики владельца (без них действуют задокументированные дефолты). */
export const actionPoliciesQuery = () =>
  queryOptions({
    queryKey: actionKeys.policies,
    queryFn: () => call(api.GET("/api/v1/action-policies")),
    staleTime: 30_000,
  });

/**
 * Эффективные режимы по классам для области: сервер сам решает «источник > вуз > сайт > глобально»
 * и возвращает, чья настройка победила. Клиент не повторяет это правило.
 */
export const effectivePoliciesQuery = (input: { sourceId?: string; spaceId?: string; url?: string }) =>
  queryOptions({
    queryKey: actionKeys.effective(input.sourceId, input.spaceId, input.url),
    queryFn: () =>
      call(
        api.GET("/api/v1/action-policies/effective", {
          params: {
            query: {
              source_id: input.sourceId,
              space_id: input.spaceId,
              url: input.url,
            },
          },
        }),
      ),
    staleTime: 15_000,
  });

/** Журнал действий, новые первыми; фильтры серверные. */
export const actionsJournalQuery = (filters: JournalFilters = {}) =>
  queryOptions({
    queryKey: actionKeys.journal(filters),
    queryFn: () =>
      call(
        api.GET("/api/v1/actions", {
          params: {
            query: {
              source_id: filters.sourceId,
              action_class: filters.actionClass,
              decision: filters.decision,
              limit: filters.limit ?? 100,
            },
          },
        }),
      ),
    staleTime: 10_000,
  });

/** Создать или обновить одну строку политики; `global` не имеет `scopeId`. */
export function upsertActionPolicy(body: ActionPolicyIn): Promise<ActionPolicy> {
  return call(api.PUT("/api/v1/action-policies", { body }));
}

/** Удалить строку: класс возвращается к дефолту (или к более широкой области). */
export function deleteActionPolicy(policyId: string): Promise<void> {
  return call(
    api.DELETE("/api/v1/action-policies/{policy_id}", { params: { path: { policy_id: policyId } } }),
  );
}

/** Решение по `ask`: `approve` разрешает выполнение ходом агента, `deny` закрывает запись. */
export function decideAction(actionId: number, decision: ActionDecision): Promise<ActionJournalEntry> {
  return call(
    api.POST("/api/v1/actions/{action_id}/decision", {
      params: path(actionId),
      body: { decision },
    }),
  );
}

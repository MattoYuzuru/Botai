import { DEFAULT_TIME_ZONE } from "@botai/design";
import { queryOptions } from "@tanstack/react-query";
import { api, call } from "./client";
import type { components } from "./schema.gen";

/**
 * Расход токенов и лимиты (backend `usage/router.py`): личный `GET /api/v1/usage` — сегодня и
 * последние семь дней, владельцу — ещё `GET /api/v1/usage/pool` с расходом каждого пользователя
 * общего ключа. Проценты клиент не считает: показывает только настоящие числа и лимит словами.
 */
type Schemas = components["schemas"];

export type Usage = Schemas["UsageOut"];
export type UsageDay = Schemas["UsageDay"];
export type PoolUsage = Schemas["PoolUsageOut"];
export type PoolUserUsage = Schemas["PoolUserUsage"];

export const usageKeys = {
  me: ["usage"] as const,
  pool: ["usage", "pool"] as const,
};

const REFRESH_MS = 60_000;

export const usageQuery = () =>
  queryOptions({
    queryKey: usageKeys.me,
    queryFn: () => call(api.GET("/api/v1/usage")),
    staleTime: 30_000,
    refetchInterval: REFRESH_MS,
  });

/** Расход пула — только владельцу: сервер отвечает 403 остальным, поэтому запрос включается по роли. */
export const poolUsageQuery = (enabled: boolean) =>
  queryOptions({
    queryKey: usageKeys.pool,
    queryFn: () => call(api.GET("/api/v1/usage/pool")),
    enabled,
    staleTime: 30_000,
    refetchInterval: REFRESH_MS,
  });

/** Сегодняшний день в поясе квоты (полночь по Москве) как `YYYY-MM-DD`, как `day` в ответе. */
export function quotaDayKey(nowMs: number, timeZone = DEFAULT_TIME_ZONE): string {
  // en-CA даёт ровно `YYYY-MM-DD`, а не локальный порядок частей.
  return new Intl.DateTimeFormat("en-CA", { timeZone }).format(nowMs);
}

export interface UsageTotals {
  tokens: number;
  costUsd: number;
  days: number;
}

/** Итог по дням: складываем только то, что сервер прислал; пустой список — нули и 0 дней. */
export function sumUsageDays(days: readonly UsageDay[]): UsageTotals {
  return days.reduce<UsageTotals>(
    (total, day) => ({
      tokens: total.tokens + day.tokens,
      costUsd: total.costUsd + day.costUsd,
      days: total.days + 1,
    }),
    { tokens: 0, costUsd: 0, days: 0 },
  );
}

/** Расход за сегодня: день, который совпал с поясом квоты; `null` — сервер его ещё не прислал. */
export function todayUsage(
  days: readonly UsageDay[],
  nowMs: number,
  timeZone = DEFAULT_TIME_ZONE,
): UsageDay | null {
  const key = quotaDayKey(nowMs, timeZone);
  return days.find((day) => day.day === key) ?? null;
}

export interface PoolTotals {
  tokensToday: number;
  costUsdToday: number;
  tokensWeek: number;
  costUsdWeek: number;
  users: number;
}

/** Итог пула по людям: сумма настоящих полей, без средних и процентов. */
export function sumPool(users: readonly PoolUserUsage[]): PoolTotals {
  return users.reduce<PoolTotals>(
    (total, user) => ({
      tokensToday: total.tokensToday + user.tokensToday,
      costUsdToday: total.costUsdToday + user.costUsdToday,
      tokensWeek: total.tokensWeek + user.tokensWeek,
      costUsdWeek: total.costUsdWeek + user.costUsdWeek,
      users: total.users + 1,
    }),
    { tokensToday: 0, costUsdToday: 0, tokensWeek: 0, costUsdWeek: 0, users: 0 },
  );
}

/** Лимит словами: ноль у сервера — «лимит не задан», а не «ноль токенов». */
export function limitKnown(tokensLimit: number): boolean {
  return Number.isFinite(tokensLimit) && tokensLimit > 0;
}

/**
 * MSW-обработчики расхода токенов (`GET /api/v1/usage`, `/usage/pool`). Форма ответов — по OpenAPI
 * (`UsageOut`, `PoolUsageOut`), поэтому карточка в e2e идёт настоящим путём. Данные синтетические:
 * семь дней с токенами и стоимостью, пул — владельцу. Адресные варианты: `?usage=empty` (ничего
 * не расходовали) и `?usage=fail` (сервер упал) для честных состояний.
 */
import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import { findUser, type MockDb } from "./db";

type S = components["schemas"];
type Variant = "empty" | "fail";

const DAY = 86_400_000;
const QUOTA_ZONE = "Europe/Moscow";
const TOKENS_LIMIT = 1_000_000;

export interface UsageHandlerOptions {
  latency?: number;
}

function readVariant(): Variant | null {
  try {
    const value = new URLSearchParams(window.location.search).get("usage");
    return value === "empty" || value === "fail" ? value : null;
  } catch {
    return null;
  }
}

function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

function dayKey(ms: number): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: QUOTA_ZONE }).format(ms);
}

/** Стоимость по ценам dev-провайдера: число небольшое, но настоящий порядок. */
function costOf(tokens: number): number {
  return Math.round(tokens * 0.000_000_4 * 1_000_000) / 1_000_000;
}

export function createUsageHandlers(db: MockDb, options: UsageHandlerOptions = {}) {
  const { latency = 0 } = options;
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const signedIn = () => Boolean(findUser(db, db.sessionUserId));
  const unauthorized = () => problem(401, "unauthorized", "Нужно войти в BotAI");
  const resetsAt = () => new Date(db.now() + 6 * 3_600_000).toISOString();

  const daily = (): S["UsageDay"][] => {
    const tokens = [12_000, 0, 38_000, 52_000, 9_000, 31_000, 41_250];
    return tokens.map((amount, index) => ({
      day: dayKey(db.now() - (tokens.length - 1 - index) * DAY),
      tokens: amount,
      costUsd: costOf(amount),
    }));
  };

  return [
    http.get("*/api/v1/usage", async () => {
      await wait();
      if (!signedIn()) return unauthorized();
      const variant = readVariant();
      if (variant === "fail") return problem(500, "internal_error", "Учёт расхода временно недоступен");
      if (variant === "empty") {
        return HttpResponse.json({
          tokensToday: 0,
          tokensLimit: TOKENS_LIMIT,
          resetsAt: resetsAt(),
          lastDays: [],
        } satisfies S["UsageOut"]);
      }
      const days = daily();
      const today = days.at(-1);
      return HttpResponse.json({
        tokensToday: today?.tokens ?? 0,
        tokensLimit: TOKENS_LIMIT,
        resetsAt: resetsAt(),
        lastDays: days,
      } satisfies S["UsageOut"]);
    }),

    http.get("*/api/v1/usage/pool", async () => {
      await wait();
      const user = findUser(db, db.sessionUserId);
      if (!user) return unauthorized();
      const variant = readVariant();
      if (variant === "fail") return problem(500, "internal_error", "Учёт расхода временно недоступен");
      if (!user.isOwner) return problem(403, "forbidden", "Расход общего ключа видит только владелец");
      if (variant === "empty") {
        return HttpResponse.json({
          tokensLimit: TOKENS_LIMIT,
          resetsAt: resetsAt(),
          users: [],
        } satisfies S["PoolUsageOut"]);
      }
      const days = daily();
      const today = days.at(-1);
      const week = days.reduce((sum, day) => sum + day.tokens, 0);
      const weekCost = days.reduce((sum, day) => sum + day.costUsd, 0);
      const users: S["PoolUserUsage"][] = [
        {
          userId: user.id,
          email: user.email,
          displayName: user.displayName,
          tokensToday: today?.tokens ?? 0,
          costUsdToday: today?.costUsd ?? 0,
          tokensWeek: week,
          costUsdWeek: Math.round(weekCost * 1_000_000) / 1_000_000,
        },
        {
          userId: "01JMKCDB0000000000000USR2",
          email: "friend@example.test",
          displayName: "Мария",
          tokensToday: 8_200,
          costUsdToday: costOf(8_200),
          tokensWeek: 74_300,
          costUsdWeek: Math.round(costOf(74_300) * 1_000_000) / 1_000_000,
        },
      ];
      return HttpResponse.json({
        tokensLimit: TOKENS_LIMIT,
        resetsAt: resetsAt(),
        users,
      } satisfies S["PoolUsageOut"]);
    }),
  ];
}

import type { SourceState } from "@botai/design";

/** Семь состояний учебных данных (styleguide §9). Они видимо различимы, и пустота не выглядит как «всё сдано». */
export type DataState = "loading" | "empty" | "ok" | "stale" | "offline" | "reauth" | "repairing";

export interface DataStateInput {
  /** Данные хотя бы раз получены (в том числе снимком из кэша). */
  hasData: boolean;
  /** Данные получены, но показывать нечего. */
  isEmpty: boolean;
  online: boolean;
  /** Показан снимок из офлайн-кэша, а не живой ответ. */
  snapshot: boolean;
  /** Последнее обновление не удалось (идут повторы в фоне), а прежние данные остались. */
  failed: boolean;
  /** Состояние источников каждого пространства в области экрана (без пространств без подключений). */
  sources: readonly SourceState[];
  /** Сервер пометил часть строк устаревшими. */
  staleItems: boolean;
}

/**
 * Какое из семи состояний у экрана. Побеждает самое важное для человека: нет сети → нужен вход →
 * вуз что-то поменял → данные устарели → пусто → всё хорошо. `loading` — только пока данных нет совсем;
 * сбой без данных остаётся скелетоном, повторы идут в фоне.
 */
export function resolveDataState(input: DataStateInput): DataState {
  if (!input.hasData) return "loading";
  if (!input.online) return "offline";
  if (input.sources.includes("reauth")) return "reauth";
  if (input.sources.includes("repairing")) return "repairing";
  if (input.snapshot || input.failed || input.staleItems || input.sources.includes("stale")) return "stale";
  if (input.isEmpty) return "empty";
  return "ok";
}

/** Состояние для `ScheduleDay`: у него четыре значения. */
export function toScheduleState(state: DataState): "loading" | "empty" | "ok" | "stale" {
  if (state === "loading") return "loading";
  if (state === "empty") return "empty";
  return state === "ok" ? "ok" : "stale";
}

/**
 * Состояние списка пар: свежие данные без пар — честное «Пар нет»; у устаревших, офлайн-снимка и данных,
 * которым нужен вход, пустота не объявляется «Пар нет» (данные могут быть неполными) — `null`, и экран
 * объясняет это сам.
 */
export function scheduleListState(
  state: DataState,
  hasItems: boolean,
): "loading" | "empty" | "ok" | "stale" | null {
  if (hasItems || state === "loading") return toScheduleState(state);
  return state === "ok" || state === "empty" ? "empty" : null;
}

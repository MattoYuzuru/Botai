import type { SourceState } from "@botai/design";
import type { Connection } from "@/api/types";

export interface SpaceHealth {
  /** Сводное состояние источников пространства; `none` — подключений ещё нет. */
  state: SourceState | "none";
  /** Время последнего снимка, мс. */
  updatedAt: number | null;
}

const PROGRESS: ReadonlySet<Connection["status"]> = new Set(["draft", "learning", "syncing", "waiting_user"]);

/**
 * Сводит состояния подключений к одному из шести видимо разных состояний источника. Приоритет:
 * нет сети → нужен повторный вход → чинится → идёт обновление → устарело/сбой → всё хорошо.
 * Отключённые подключения не считаются.
 */
export function summarizeConnections(connections: readonly Connection[], online: boolean): SpaceHealth {
  const live = connections.filter((connection) => connection.status !== "disabled");
  const times = live.map((connection) =>
    connection.lastSyncAt ? Date.parse(connection.lastSyncAt) : Number.NaN,
  );
  const valid = times.filter(Number.isFinite);
  const updatedAt = valid.length ? Math.max(...valid) : null;
  if (live.length === 0) return { state: "none", updatedAt };
  if (!online) return { state: "offline", updatedAt };
  const has = (predicate: (c: Connection) => boolean) => live.some(predicate);
  if (has((c) => c.status === "needs_reauth")) return { state: "reauth", updatedAt };
  if (has((c) => c.status === "repairing")) return { state: "repairing", updatedAt };
  if (has((c) => PROGRESS.has(c.status))) return { state: "syncing", updatedAt };
  if (has((c) => c.status === "stale" || c.status === "error")) return { state: "stale", updatedAt };
  return { state: "ok", updatedAt };
}

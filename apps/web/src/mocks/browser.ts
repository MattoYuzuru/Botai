import { setupWorker } from "msw/browser";
import { createDb, restoreDb, type Scenario } from "./db";
import { createHandlers } from "./handlers";

const SCENARIOS: readonly Scenario[] = ["fresh", "empty", "connected", "reauth", "stale"];

/** Сценарий из адреса (`?scenario=connected`) запоминается на вкладку. Без него — «fresh»: вход по приглашению. */
function readScenario(): Scenario | null {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("scenario");
    if (fromUrl && (SCENARIOS as readonly string[]).includes(fromUrl)) {
      window.sessionStorage.setItem("botai.mock.scenario", fromUrl);
      window.sessionStorage.removeItem("botai.mock.db");
      return fromUrl as Scenario;
    }
    return (window.sessionStorage.getItem("botai.mock.scenario") as Scenario | null) ?? null;
  } catch {
    return null;
  }
}

/** Задержка ответов для разработки: видны скелетоны и индикаторы. `?latency=0` — без задержки (e2e). */
function readLatency(): number {
  const raw = new URLSearchParams(window.location.search).get("latency");
  const value = raw === null ? Number.NaN : Number(raw);
  return Number.isFinite(value) ? value : 250;
}

export async function startMocks(): Promise<void> {
  const scenario = readScenario();
  const db = restoreDb() ?? createDb(scenario ?? "fresh");
  const worker = setupWorker(...createHandlers(db, { latency: readLatency(), persist: true }));
  await worker.start({ onUnhandledRequest: "bypass", quiet: true });
}

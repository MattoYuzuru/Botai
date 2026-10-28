import { setupWorker } from "msw/browser";
import { useChatJobs } from "@/features/chat/chat-jobs";
import { useActiveJob } from "@/features/jobs/active-job";
import { JOB_VARIANTS, type JobVariant } from "./connect-script";
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
      window.sessionStorage.removeItem("botai.mock.chat");
      // Stores may already be hydrated by route imports; reset them together with the synthetic DB.
      useActiveJob.getState().clear();
      useChatJobs.getState().clearAll();
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

/** Сценарий подключения вуза (`?job=wrong-password|push|failure|approval`), запоминается на вкладку. */
function readJobVariant(): JobVariant {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get("job");
    if (fromUrl && (JOB_VARIANTS as readonly string[]).includes(fromUrl)) {
      window.sessionStorage.setItem("botai.mock.job", fromUrl);
      return fromUrl as JobVariant;
    }
    const stored = window.sessionStorage.getItem("botai.mock.job");
    return stored && (JOB_VARIANTS as readonly string[]).includes(stored) ? (stored as JobVariant) : "basic";
  } catch {
    return "basic";
  }
}

/** Скорость подключения: `?pace=0` — мгновенно (e2e), по умолчанию — как для человека. */
function readPace(): number {
  const raw = new URLSearchParams(window.location.search).get("pace");
  const value = raw === null ? Number.NaN : Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : 1;
}

export async function startMocks(): Promise<void> {
  const scenario = readScenario();
  const db = restoreDb() ?? createDb(scenario ?? "fresh");
  db.jobVariant = readJobVariant();
  const worker = setupWorker(
    ...createHandlers(db, { latency: readLatency(), persist: true, jobPace: readPace() }),
  );
  await worker.start({ onUnhandledRequest: "bypass", quiet: true });
}

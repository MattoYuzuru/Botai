import { setupServer } from "msw/node";
import { createDb, type MockDb, type Scenario } from "@/mocks/db";
import { createHandlers } from "@/mocks/handlers";

/** Сервер MSW для тестов: свежая «база» по сценарию, без задержек; `jobPace` растягивает паузы сценария подключения. */
export function createTestServer(scenario: Scenario = "fresh", options: { jobPace?: number } = {}) {
  const db: MockDb = createDb(scenario);
  const server = setupServer(...createHandlers(db, { jobPace: options.jobPace ?? 0 }));
  return { db, server };
}

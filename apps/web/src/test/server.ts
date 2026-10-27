import { setupServer } from "msw/node";
import { createDb, type MockDb, type Scenario } from "@/mocks/db";
import { createHandlers } from "@/mocks/handlers";

/** Сервер MSW для тестов: свежая «база» по сценарию, без задержек. */
export function createTestServer(scenario: Scenario = "fresh") {
  const db: MockDb = createDb(scenario);
  const server = setupServer(...createHandlers(db));
  return { db, server };
}

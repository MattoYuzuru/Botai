import { setupServer } from "msw/node";
import { createDataHandlers, type DataVariant } from "@/mocks/data-handlers";
import { createDb, type MockDb, type Scenario } from "@/mocks/db";
import { createHandlers } from "@/mocks/handlers";

/** «Сейчас» тестов экранов данных: вторник, 27 октября 2026, 17:40 по Москве. Данные мока строятся от него. */
export const NOW = Date.UTC(2026, 9, 27, 14, 40);

/**
 * Сервер MSW для экранов данных: фиксированное «сейчас», сценарий подключений и вариант данных
 * (`quiet`, `repairing`, `waiting`, `error`). Обработчики варианта идут первыми и перекрывают общие.
 */
export function createDataServer(scenario: Scenario = "connected", variant: DataVariant | null = null) {
  const db: MockDb = createDb(scenario, () => NOW);
  const server = setupServer(
    ...createDataHandlers(db, { variant }),
    ...createHandlers(db),
  );
  return { db, server };
}

import createClient from "openapi-fetch";
import { observeDataReset } from "@/features/auth/data-reset";
import { NetworkError, parseProblem } from "./errors";
import type { paths } from "./schema.gen";

/**
 * Типизированный клиент API: типы генерируются из OpenAPI бэкенда (`make gen`).
 * Базовый адрес — origin страницы: в dev Vite проксирует `/api`, в проде приложение и API на одном домене.
 */
export const api = createClient<paths>({
  baseUrl: typeof window === "undefined" ? "http://localhost" : window.location.origin,
  credentials: "include",
  // Берём fetch в момент вызова: тесты и мок-режим подменяют глобальный fetch после создания клиента.
  fetch: async (request) => {
    const response = await globalThis.fetch(request);
    if (new URL(request.url).pathname === "/api/v1/me") await observeDataReset(response);
    return response;
  },
});

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/**
 * Разворачивает ответ openapi-fetch: данные или исключение. Сбой связи — `NetworkError`,
 * ответ сервера с ошибкой — `ApiError` с кодом из problem+json.
 */
export async function call<T>(request: Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await request;
  } catch (cause) {
    throw new NetworkError(cause);
  }
  if (!result.response.ok) throw parseProblem(result.response.status, result.error);
  return result.data as T;
}

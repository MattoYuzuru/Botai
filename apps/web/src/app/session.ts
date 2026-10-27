import type { QueryClient } from "@tanstack/react-query";
import { redirect } from "@tanstack/react-router";
import { isNetworkError, isUnauthorized } from "@/api/errors";
import { meQuery } from "@/api/queries";

/**
 * Защита маршрутов приложения. Нет сессии — на вход. Нет сети — не выгоняем на вход: человек увидит
 * последний снимок, а сессия проверится, когда связь вернётся.
 */
export async function requireSession(queryClient: QueryClient) {
  try {
    // Без повторов: проверка сессии не должна подвешивать переход, пока сеть недоступна.
    return await queryClient.ensureQueryData({ ...meQuery, retry: false });
  } catch (error) {
    if (isUnauthorized(error)) throw redirect({ to: "/login" });
    if (isNetworkError(error)) return null;
    throw error;
  }
}

/** Страницы входа и регистрации: если сессия уже есть — сразу в приложение. */
export async function redirectIfSignedIn(queryClient: QueryClient) {
  try {
    await queryClient.ensureQueryData({ ...meQuery, retry: false });
  } catch {
    return;
  }
  throw redirect({ to: "/" });
}

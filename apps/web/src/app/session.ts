import type { QueryClient } from "@tanstack/react-query";
import { redirect } from "@tanstack/react-router";
import { isNetworkError, isUnauthorized } from "@/api/errors";
import { meQuery } from "@/api/queries";

/** Куда вернуться после входа: только ссылка на чужой чат (`/shared/…`), чтобы друг попал туда, куда шёл. */
export function returnPath(href: string | undefined): string | undefined {
  return href?.startsWith("/shared/") ? href : undefined;
}

/**
 * Защита маршрутов приложения. Нет сессии — на вход. Нет сети — не выгоняем на вход: человек увидит
 * последний снимок, а сессия проверится, когда связь вернётся.
 */
export async function requireSession(queryClient: QueryClient, href?: string) {
  try {
    // Без повторов: проверка сессии не должна подвешивать переход, пока сеть недоступна.
    return await queryClient.ensureQueryData({ ...meQuery, retry: false });
  } catch (error) {
    if (isUnauthorized(error)) {
      const next = returnPath(href);
      throw redirect({ to: "/login", search: next ? { next } : {} });
    }
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

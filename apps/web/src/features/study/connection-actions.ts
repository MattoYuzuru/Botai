import type { QueryClient } from "@tanstack/react-query";
import { api, call } from "@/api/client";
import { queryKeys } from "@/api/queries";
import type { SecretKind } from "@/features/data/types";

/**
 * Действия над подключением. Секретное значение (ссылка календаря, ключ доступа) уходит прямым вызовом, а не
 * мутацией React Query: так оно не остаётся в кэше мутаций и в состоянии компонента. Ответ сервера значения не
 * возвращает, и экран его не показывает.
 */
export async function putSecret(connectionId: string, kind: SecretKind, value: string): Promise<void> {
  await call(
    api.PUT("/api/v1/connections/{connection_id}/secrets", {
      params: { path: { connection_id: connectionId } },
      body: { kind, value },
    }),
  );
}

export async function forgetPassword(connectionId: string): Promise<void> {
  await call(
    api.DELETE("/api/v1/connections/{connection_id}/password", {
      params: { path: { connection_id: connectionId } },
    }),
  );
}

export async function deleteConnection(connectionId: string): Promise<void> {
  await call(
    api.DELETE("/api/v1/connections/{connection_id}", { params: { path: { connection_id: connectionId } } }),
  );
}

export async function deleteDocument(documentId: string): Promise<void> {
  await call(
    api.DELETE("/api/v1/documents/{document_id}", { params: { path: { document_id: documentId } } }),
  );
}

/** Подключения вуза и всё, что от них зависит: расписание, сроки, списки разделов и счётчики. */
export async function refreshAfterChange(queryClient: QueryClient, spaceId: string): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.connections(spaceId) }),
    queryClient.invalidateQueries({ queryKey: queryKeys.sections(spaceId) }),
    queryClient.invalidateQueries({ queryKey: ["spaces", spaceId, "documents"] }),
    queryClient.invalidateQueries({ queryKey: queryKeys.today }),
    ...["schedule", "deadlines", "courses", "grades", "people", "news"].map((root) =>
      queryClient.invalidateQueries({ queryKey: [root] }),
    ),
  ]);
}

/** Вид ключа для человека: без слов «токен», «API». */
export const SECRET_KINDS: readonly {
  value: SecretKind;
  label: string;
  description: string;
  field: string;
  hint: string;
  /** Тост после сохранения, в прошедшем времени. */
  saved: string;
}[] = [
  {
    value: "ical_url",
    label: "Ссылка на календарь",
    description: "Ссылка на экспорт расписания из личного кабинета",
    field: "Ссылка на календарь",
    hint: "Начинается с https:// или webcal://",
    saved: "Ссылка на календарь сохранена",
  },
  {
    value: "token",
    label: "Ключ доступа",
    description: "Ключ из настроек учебной платформы",
    field: "Ключ доступа",
    hint: "Его выдаёт сама платформа, обычно в профиле",
    saved: "Ключ доступа сохранён",
  },
  {
    value: "api_key",
    label: "Ключ сервиса",
    description: "Если источник выдаёт отдельный ключ",
    field: "Ключ сервиса",
    hint: "Вставьте ключ целиком",
    saved: "Ключ сервиса сохранён",
  },
];

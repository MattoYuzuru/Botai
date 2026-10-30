import type { QueryClient } from "@tanstack/react-query";
import { api, call } from "@/api/client";
import { queryKeys } from "@/api/queries";
import type { SecretKind } from "@/features/data/types";

/**
 * Действия над подключением. Секретное значение (ключ доступа, ключ сервиса) уходит прямым вызовом, а не
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

/** Отключение обратимо: данные источника уходят из видов, ничего не удаляется; включить можно там же (FR-054). */
export async function setSourceEnabled(connectionId: string, enabled: boolean): Promise<void> {
  const path = { connection_id: connectionId };
  await call(
    enabled
      ? api.POST("/api/v1/connections/{connection_id}/enable", { params: { path } })
      : api.POST("/api/v1/connections/{connection_id}/disable", { params: { path } }),
  );
}

/** Основной источник расписания вуза: при расхождениях решает он. `null` — вернуть выбор по умолчанию. */
export async function setPrimarySource(spaceId: string, connectionId: string | null): Promise<void> {
  await call(
    api.PUT("/api/v1/spaces/{space_id}/primary-source", {
      params: { path: { space_id: spaceId } },
      body: { connectionId },
    }),
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

/** Вид ключа для человека: без слов «токен», «API». Ссылка календаря сюда не входит: её принимает защищённое поле задачи в чате. */
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

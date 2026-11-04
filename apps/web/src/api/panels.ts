import { queryOptions } from "@tanstack/react-query";
import { api, call } from "./client";
import type { components } from "./schema.gen";

/**
 * Панели «Учёбы» (D-039, docs/v4/23-panels-backend.md): типы из сгенерированного OpenAPI, запросы без
 * ручного fetch. Панель — декларативный конфиг представления; данные приходят готовыми в `data`, клиент
 * их не пересчитывает. Как рисовать — `features/study/panels`.
 */
type Schemas = components["schemas"];

export type PanelOut = Schemas["PanelOut"];
export type PanelPatch = Schemas["PanelPatch"];
export type PanelSpaceOut = Schemas["PanelSpaceOut"];
export type PanelType = PanelOut["type"];
export type PanelStatus = PanelOut["status"];
/** Имена иконок, которые бэкенд разрешает в `spec.icon`; vitest сверяет их с реестром дизайна. */
export type PanelIcon = PanelOut["spec"]["icon"];
export type GradesData = Schemas["GradesData"];
export type GradePointOut = Schemas["GradePointOut"];
export type ProfileData = Schemas["ProfileData"];
export type RankingData = Schemas["RankingData"];

export const panelKeys = {
  all: ["panels"] as const,
  list: (status: "active" | "proposed") => ["panels", "list", status] as const,
};

const PANELS_REFRESH_MS = 60_000;

/**
 * Панели всех вузов пользователя. Вуз выбирается на клиенте (`space_id` не передаётся): один ключ кеша
 * на любой выбор в переключателе, переключение вуза не вызывает повторного запроса и скелетона.
 */
export const panelsQuery = (status: "active" | "proposed" = "active") =>
  queryOptions({
    queryKey: panelKeys.list(status),
    queryFn: () => call(api.GET("/api/v1/panels", { params: { query: { status } } })),
    staleTime: 30_000,
    refetchInterval: PANELS_REFRESH_MS,
  });

export function patchPanel(id: string, body: PanelPatch): Promise<PanelOut> {
  return call(api.PATCH("/api/v1/panels/{panel_id}", { params: { path: { panel_id: id } }, body }));
}

export function acceptPanel(id: string): Promise<PanelOut> {
  return call(api.POST("/api/v1/panels/{panel_id}/accept", { params: { path: { panel_id: id } } }));
}

export function dismissPanel(id: string): Promise<PanelOut> {
  return call(api.POST("/api/v1/panels/{panel_id}/dismiss", { params: { path: { panel_id: id } } }));
}

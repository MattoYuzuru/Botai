import type { IconName } from "@botai/design";

/**
 * Внутренние ссылки источников ответа агента. Сервер кладёт в `sources[].url` адреса вида
 * `botai://documents/<id>` (загруженный или подключённый документ) и `botai://connections/<id>` (подключение вуза):
 * их нельзя открыть как сайт, они ведут в «Учёбу». Всё остальное, включая `botai://sources/N` (сервер
 * подставляет его, когда у источника нет настоящего адреса), внутренней ссылкой не считается.
 */
export type InternalLink = { kind: "document" | "connection"; id: string };

const PATTERN = /^botai:\/\/(documents|connections)\/([A-Za-z0-9_-]{1,64})$/;

export function parseInternalLink(url: string | null | undefined): InternalLink | null {
  const match = PATTERN.exec((url ?? "").trim());
  if (!match) return null;
  return { kind: match[1] === "documents" ? "document" : "connection", id: match[2] as string };
}

/**
 * Раздел «Учёбы», где лежит документ или подключение: `/study/documents`, `/study/connections`. Экран раздела
 * хранит открытую карточку у себя и пока не читает идентификатор из адреса, поэтому ссылка ведёт в нужный раздел.
 */
export function internalHref(link: InternalLink): string {
  return link.kind === "document" ? "/study/documents" : "/study/connections";
}

export function internalLabel(link: InternalLink): string {
  return link.kind === "document" ? "Открыть документ в «Учёбе»" : "Открыть подключение в «Учёбе»";
}

export function internalIcon(link: InternalLink): IconName {
  return link.kind === "document" ? "document" : "connection";
}

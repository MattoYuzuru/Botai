import type { IconName } from "@botai/design";
import { attachmentContentUrl } from "@/api/v3";

/**
 * Внутренние ссылки агента (§3.1): `botai://threads/<id>` ведёт в тред, `botai://connections/<id>` — к подключению,
 * `botai://documents/<id>` — к документу, `botai://attachments/<id>` открывает вложение во вьюере. Их нельзя
 * открыть как сайт. Всё остальное, включая `botai://sources/N` (сервер подставляет его, когда у источника нет
 * настоящего адреса), внутренней ссылкой не считается.
 */
export type InternalLink = {
  kind: "thread" | "document" | "connection" | "attachment";
  id: string;
};

const PATTERN = /^botai:\/\/(threads|documents|connections|attachments)\/([A-Za-z0-9_-]{1,64})$/;

const KINDS = {
  threads: "thread",
  documents: "document",
  connections: "connection",
  attachments: "attachment",
} as const;

export function parseInternalLink(url: string | null | undefined): InternalLink | null {
  const match = PATTERN.exec((url ?? "").trim());
  if (!match) return null;
  return { kind: KINDS[match[1] as keyof typeof KINDS], id: match[2] as string };
}

/**
 * Куда ведёт ссылка в приложении. Тред — `/chat?thread=<id>`. Документы и подключения лежат в «Учёбе»: экран
 * раздела хранит открытую карточку у себя и пока не читает идентификатор из адреса, поэтому ведём в раздел.
 * Вложение открывается во вьюере, а адрес — его содержимое (средняя кнопка мыши откроет файл).
 */
export function internalHref(link: InternalLink): string {
  switch (link.kind) {
    case "thread":
      return `/chat?thread=${encodeURIComponent(link.id)}`;
    case "document":
      return "/study/documents";
    case "connection":
      return "/study/connections";
    case "attachment":
      return attachmentContentUrl(link.id);
  }
}

export function internalLabel(link: InternalLink): string {
  switch (link.kind) {
    case "thread":
      return "Открыть чат";
    case "document":
      return "Открыть документ в «Учёбе»";
    case "connection":
      return "Открыть подключение в «Учёбе»";
    case "attachment":
      return "Открыть вложение";
  }
}

export function internalIcon(link: InternalLink): IconName {
  switch (link.kind) {
    case "thread":
      return "chat";
    case "document":
      return "document";
    case "connection":
      return "connection";
    case "attachment":
      return "attach";
  }
}

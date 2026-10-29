import type { Connection } from "@/api/types";

/**
 * Переходы из экранов данных в чат (agent-hub-v3 §3.9). Других способов «перенаправить в подключение» нет:
 * `/chat?thread=<id>` открывает тред, `/chat?draft=<текст>` — новый тред с заполненным полем ввода, которое
 * человек отправляет сам.
 */
export interface ChatLink {
  to: "/chat";
  search: { thread?: string; draft?: string };
}

export const chatThreadLink = (threadId: string): ChatLink => ({ to: "/chat", search: { thread: threadId } });
export const chatDraftLink = (draft: string): ChatLink => ({ to: "/chat", search: { draft } });

/** «Подключить ещё вуз»: фраза оставлена открытой, название вуза или адрес допишет человек. */
export const CONNECT_UNIVERSITY_DRAFT = "Давай подключим вуз ";

export const connectUniversityLink = (): ChatLink => chatDraftLink(CONNECT_UNIVERSITY_DRAFT);

/** «Добавить источник» вуза: готовая просьба с названием вуза; какой именно источник, допишет человек. */
export const addSourceDraft = (university: string): string =>
  `Давай добавим для ${university} ещё один источник: `;

export const addSourceLink = (university: string): ChatLink => chatDraftLink(addSourceDraft(university));

/** Просьба по состоянию источника, когда у него нет своего треда задачи. */
function attentionDraft(connection: Connection, university: string): string {
  const source = `«${connection.title}» (${university})`;
  switch (connection.status) {
    case "needs_reauth":
      return `Войди снова в ${source}`;
    case "waiting_user":
      return `Продолжим подключение ${source}`;
    case "error":
      return `Почему не обновляется ${source}? Посмотри и почини`;
    default:
      return `Что с источником ${source}?`;
  }
}

/**
 * Куда вести плашку источника: в тред его задачи, а если треда нет — в новый чат с готовой просьбой.
 * `university` — как называть вуз в просьбе («НИУ ВШЭ»).
 */
export function connectionChatLink(connection: Connection, university: string): ChatLink {
  return connection.threadId
    ? chatThreadLink(connection.threadId)
    : chatDraftLink(attentionDraft(connection, university));
}

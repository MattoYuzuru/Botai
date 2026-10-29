import { createFileRoute } from "@tanstack/react-router";
import { ChatScreen } from "@/features/chat/chat-screen";

/** Заготовка в поле ввода не длиннее лимита сообщения: длинный адрес не раздувает историю браузера. */
const MAX_DRAFT = 2000;

/**
 * Чат. Открытый чат хранится в адресе (§3.9): `/chat` — последний чат, `/chat?thread=<id>` — выбранный,
 * `/chat?thread=new` — новый, `/chat?draft=<текст>` — новый чат с заполненным полем ввода (ничего не отправляется),
 * `/chat?thread=<id>&draft=<текст>` — то же в существующем чате. Так работают «назад», уведомления со ссылкой на
 * нужный чат и открытие чата в новой вкладке.
 */
export const Route = createFileRoute("/_app/chat")({
  validateSearch: (search: Record<string, unknown>): { thread?: string; draft?: string } => ({
    thread: typeof search.thread === "string" && search.thread !== "" ? search.thread : undefined,
    draft:
      typeof search.draft === "string" && search.draft !== "" ? search.draft.slice(0, MAX_DRAFT) : undefined,
  }),
  component: ChatScreen,
});

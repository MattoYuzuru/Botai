import { createFileRoute } from "@tanstack/react-router";
import { ChatScreen } from "@/features/chat/chat-screen";

/**
 * Чат. Открытый чат хранится в адресе: `/chat` — последний чат, `/chat?thread=<id>` — выбранный,
 * `/chat?thread=new` — новый. Так работают «назад» и открытие чата в новой вкладке.
 */
export const Route = createFileRoute("/_app/chat")({
  validateSearch: (
    search: Record<string, unknown>,
  ): { thread?: string; connect?: string; space?: string; university?: string } => ({
    thread: typeof search.thread === "string" && search.thread !== "" ? search.thread : undefined,
    connect:
      typeof search.connect === "string" && /^[0-9A-HJKMNP-TV-Z]{26}$/.test(search.connect)
        ? search.connect
        : undefined,
    space: typeof search.space === "string" && search.space !== "" ? search.space : undefined,
    university:
      typeof search.university === "string" && search.university !== "" ? search.university : undefined,
  }),
  component: ChatScreen,
});

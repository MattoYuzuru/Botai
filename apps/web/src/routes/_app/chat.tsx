import { createFileRoute } from "@tanstack/react-router";
import { ChatScreen } from "@/features/chat/chat-screen";

/**
 * Чат. Открытый чат хранится в адресе: `/chat` — последний чат, `/chat?thread=<id>` — выбранный,
 * `/chat?thread=new` — новый. Так работают «назад» и открытие чата в новой вкладке.
 */
export const Route = createFileRoute("/_app/chat")({
  validateSearch: (search: Record<string, unknown>): { thread?: string } => ({
    thread: typeof search.thread === "string" && search.thread !== "" ? search.thread : undefined,
  }),
  component: ChatScreen,
});

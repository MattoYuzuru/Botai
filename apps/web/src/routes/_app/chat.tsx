import { createFileRoute } from "@tanstack/react-router";
import { ChatScreen } from "@/features/chat/chat-screen";

/**
 * Чат. Открытый чат хранится в адресе: `/chat` — последний чат, `/chat?thread=<id>` — выбранный,
 * `/chat?thread=new` — новый, `/chat?draft=<текст>` — новый чат с заполненным полем ввода (не отправляется
 * само; контракт переходов agent-hub-v3 §3.9). Так работают «назад» и открытие чата в новой вкладке.
 */
export const Route = createFileRoute("/_app/chat")({
  validateSearch: (
    search: Record<string, unknown>,
  ): { thread?: string; draft?: string; connect?: string; space?: string; university?: string } => ({
    thread: typeof search.thread === "string" && search.thread !== "" ? search.thread : undefined,
    draft: typeof search.draft === "string" && search.draft !== "" ? search.draft : undefined,
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

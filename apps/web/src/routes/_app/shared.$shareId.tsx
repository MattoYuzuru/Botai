import { createFileRoute } from "@tanstack/react-router";
import { SharedChatScreen } from "@/features/shared-chat/shared-chat-screen";

/**
 * Чат, которым поделились (`/shared/<id>`): только чтение для любого пользователя BotAI. Без входа — на вход и
 * обратно сюда (`returnPath`).
 */
export const Route = createFileRoute("/_app/shared/$shareId")({
  component: function SharedChatRoute() {
    const { shareId } = Route.useParams();
    return <SharedChatScreen shareId={shareId} />;
  },
});

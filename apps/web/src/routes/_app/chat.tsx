import { createFileRoute } from "@tanstack/react-router";
import { TabPlaceholder } from "@/features/common/tab-placeholder";

export const Route = createFileRoute("/_app/chat")({
  component: () => (
    <TabPlaceholder
      title="Чат"
      icon="chat"
      description="Здесь можно будет спросить агента про пары, сроки и документы."
    />
  ),
});

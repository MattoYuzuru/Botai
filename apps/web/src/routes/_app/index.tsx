import { createFileRoute } from "@tanstack/react-router";
import { TabPlaceholder } from "@/features/common/tab-placeholder";

export const Route = createFileRoute("/_app/")({
  component: () => (
    <TabPlaceholder
      title="Сегодня"
      icon="today"
      description="Пары сегодня и ближайшие сроки из всех вузов появятся здесь."
    />
  ),
});

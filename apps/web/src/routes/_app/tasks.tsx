import { createFileRoute } from "@tanstack/react-router";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { TabPlaceholder } from "@/features/common/tab-placeholder";

export const Route = createFileRoute("/_app/tasks")({
  component: () => (
    <TabPlaceholder
      title="Задания"
      icon="deadline"
      description="Дедлайны и домашние задания по группам появятся здесь."
      trailing={<ConnectedSpaceSwitcher />}
    />
  ),
});

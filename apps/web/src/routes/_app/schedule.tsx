import { createFileRoute } from "@tanstack/react-router";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { TabPlaceholder } from "@/features/common/tab-placeholder";

export const Route = createFileRoute("/_app/schedule")({
  component: () => (
    <TabPlaceholder
      title="Расписание"
      icon="schedule"
      description="Неделя и день с занятиями из ваших вузов появятся здесь."
      trailing={<ConnectedSpaceSwitcher />}
    />
  ),
});

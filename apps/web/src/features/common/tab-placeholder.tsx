import { EmptyState, type IconName, SkeletonText } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { spacesQuery } from "@/api/queries";
import { Screen } from "@/app/screen";
import { ConnectPrompt } from "@/features/today/connect-prompt";

/**
 * Каркас вкладки до наполнения: загрузка → онбординг без вуза → «раздел готовится». Экраны данных
 * (расписание, задания, чат) заменяют его в следующих срезах; пустота не выдаётся за «всё сдано».
 */
export function TabPlaceholder({
  title,
  icon,
  description,
  trailing,
}: {
  title: string;
  icon: IconName;
  description: string;
  trailing?: ReactNode;
}) {
  const spaces = useQuery(spacesQuery);
  return (
    <Screen title={title} trailing={trailing}>
      {spaces.isPending ? (
        <SkeletonText lines={4} label="Загрузка" />
      ) : spaces.data && spaces.data.length > 0 ? (
        <EmptyState icon={icon} title="Раздел готовится" description={description} />
      ) : (
        <ConnectPrompt />
      )}
    </Screen>
  );
}

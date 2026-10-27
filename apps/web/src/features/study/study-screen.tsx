import { EmptyState, SkeletonText } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { spacesQuery } from "@/api/queries";
import { Screen } from "@/app/screen";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { AccountSettings } from "@/features/settings/account";
import { ConnectPrompt } from "@/features/today/connect-prompt";

/** «Учёба» (`/study`): пространства вузов и их разделы; профиль и настройки — внизу экрана. */
export function StudyScreen() {
  const spaces = useQuery(spacesQuery);
  return (
    <Screen title="Учёба" trailing={<ConnectedSpaceSwitcher />}>
      {spaces.isPending ? (
        <SkeletonText lines={4} label="Загрузка" />
      ) : spaces.data && spaces.data.length > 0 ? (
        <EmptyState
          icon="university"
          title="Раздел готовится"
          description="Предметы, оценки, преподаватели и документы появятся здесь."
        />
      ) : (
        <ConnectPrompt />
      )}
      <AccountSettings />
    </Screen>
  );
}

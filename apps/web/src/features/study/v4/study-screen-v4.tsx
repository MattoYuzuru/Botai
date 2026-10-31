import { Skeleton, useNow, useOnline } from "@botai/design";
import { useState } from "react";
import { Screen } from "@/app/screen";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { resolveDataState } from "@/features/data/data-state";
import { ScreenSyncIndicator } from "@/features/data/source-notices";
import { type SpaceInfo, useScope, useSpaceDirectory } from "@/features/data/use-space-directory";
import { AccountSettings } from "@/features/settings/account";
import { ConnectPrompt } from "@/features/today/connect-prompt";
import { SourceCardV4 } from "./source-card-v4";
import { UniversityPanelV4 } from "./university-panel-v4";
import type { StudySource } from "./use-study-sources";

/**
 * «Учёба» (03-sources-and-ux.md §5, D-013) — единственный экран раздела, без фича-флага: вуз — карточка,
 * источник — живая запись с видами и статусом. Отдельных разделов «Источники», «Документы и вложения» и
 * пространства «Мои материалы» нет: документы — артефакты источников в карточке вуза, источники — здесь,
 * данные — производные разделы ниже.
 */
export function StudyScreenV4() {
  const directory = useSpaceDirectory();
  const scope = useScope(directory);
  const nowMs = useNow(30_000);
  const online = useOnline();
  const [opened, setOpened] = useState<{ source: StudySource; space: SpaceInfo } | null>(null);
  // Вуз — единственная единица экрана: пространства без вуза в директорию не попадают (useSpaceDirectory).
  const universities = scope.spaces;
  const state = resolveDataState({
    hasData: directory.connectionsLoaded,
    isEmpty: false,
    online,
    snapshot: false,
    failed: false,
    sources: universities.map((space) => space.health.state).filter((value) => value !== "none"),
    staleItems: false,
  });
  const trailing = (
    <>
      <ScreenSyncIndicator />
      <ConnectedSpaceSwitcher />
    </>
  );

  return (
    <Screen title="Учёба" trailing={trailing}>
      {!directory.loaded ? (
        <div
          data-screen="study"
          data-state="loading"
          role="status"
          aria-busy="true"
          className="flex flex-col gap-3"
        >
          <span className="sr-only">Загрузка</span>
          <Skeleton width="40%" className="h-6" />
          <Skeleton className="h-14 rounded-lg" />
          <Skeleton className="h-14 rounded-lg" />
        </div>
      ) : universities.length === 0 ? (
        <div data-screen="study" data-state="empty">
          <ConnectPrompt />
        </div>
      ) : (
        <div data-screen="study" data-state={state} className="flex flex-col gap-6">
          {universities.map((space) => (
            <UniversityPanelV4
              key={space.id}
              space={space}
              nowMs={nowMs}
              onOpenSource={(source) => setOpened({ source, space })}
            />
          ))}
          <p className="px-1 type-caption text-fg-subtle">
            Вуза нет в списке? Напишите агенту в чате — он подключит новый.
          </p>
        </div>
      )}
      <SourceCardV4
        source={opened?.source ?? null}
        space={opened?.space}
        nowMs={nowMs}
        onClose={() => setOpened(null)}
      />
      <AccountSettings />
    </Screen>
  );
}

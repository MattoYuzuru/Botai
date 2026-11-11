import { SegmentedControl, Skeleton, useNow, useOnline } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { Screen } from "@/app/screen";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { resolveDataState } from "@/features/data/data-state";
import { gradesQuery, updatesQuery } from "@/features/data/queries";
import { ScreenSyncIndicator } from "@/features/data/source-notices";
import { type SpaceInfo, useScope, useSpaceDirectory } from "@/features/data/use-space-directory";
import { AccountSettings } from "@/features/settings/account";
import { ConnectPrompt } from "@/features/today/connect-prompt";
import { panelsForScope } from "../panels/model";
import { usePanelsData } from "../panels/use-panels";
import { PerformanceView } from "../performance/performance-view";
import { SourceCardV4 } from "./source-card-v4";
import { UniversityPanelV4 } from "./university-panel-v4";
import type { StudySource } from "./use-study-sources";

/** Раздел экрана «Учёба» (D-046): успеваемость или источники; хранится в адресе (`?view=`). */
export type StudyView = "grades" | "sources";

export const STUDY_VIEWS: readonly { value: StudyView; label: string }[] = [
  { value: "grades", label: "Успеваемость" },
  { value: "sources", label: "Источники" },
];

export const isStudyView = (value: unknown): value is StudyView => value === "grades" || value === "sources";

/**
 * «Учёба» (03-sources-and-ux.md §5, D-013, D-046) — единственный экран раздела. Сверху переключатель
 * «Успеваемость / Источники»: выбор живёт в адресе (`?view=`), поэтому переживает перезагрузку и «назад».
 * Без выбора экран открывается успеваемостью, если у студента есть оценки, «Новое» или панели, иначе —
 * источниками. «Источники» — вузы, их источники, документы, разделы и настройки, как раньше.
 */
export function StudyScreenV4({ view: requested }: { view?: StudyView }) {
  const directory = useSpaceDirectory();
  const scope = useScope(directory);
  const nowMs = useNow(30_000);
  const online = useOnline();
  const navigate = useNavigate();
  const [opened, setOpened] = useState<{ source: StudySource; space: SpaceInfo } | null>(null);
  const ready = directory.loaded && !directory.none;
  const grades = useQuery({ ...gradesQuery(scope.spaceId), enabled: ready });
  const updates = useQuery({ ...updatesQuery(scope.spaceId), enabled: ready });
  const panels = usePanelsData();
  const spaceIds = scope.spaces.map((space) => space.id);
  const hasPanels =
    panelsForScope(panels.active ?? [], { spaceIds, grouped: false }).length > 0 ||
    panelsForScope(panels.proposed, { spaceIds, grouped: false }).length > 0;

  // В v4 пространство — всегда вуз: каждая единица директории — вуз со своими источниками.
  const universities = scope.spaces;
  const gradeRows = grades.data?.data ?? [];
  const updateRows = updates.data?.data ?? [];
  // Оценки — главное содержимое: пока не пришли, выбор по умолчанию не делаем (скелетон «Успеваемости»).
  const gradesPending = ready && grades.isPending;
  const hasPerformance = gradeRows.length > 0 || updateRows.length > 0 || hasPanels;
  const view: StudyView = requested ?? (gradesPending || hasPerformance ? "grades" : "sources");
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
  const select = (next: StudyView) =>
    void navigate({ to: "/study", search: { view: next }, replace: true, resetScroll: false });

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
          <Skeleton className="h-11 rounded-lg" />
          <Skeleton className="h-28 rounded-lg" />
          <Skeleton className="h-28 rounded-lg" />
        </div>
      ) : universities.length === 0 ? (
        <div data-screen="study" data-state="empty">
          <ConnectPrompt />
        </div>
      ) : (
        <div data-screen="study" data-state={state} data-study-view={view} className="flex flex-col gap-6">
          <SegmentedControl
            label="Раздел «Учёбы»"
            options={STUDY_VIEWS}
            value={view}
            onValueChange={select}
            className="lg:max-w-reading"
          />
          {view === "grades" ? (
            <PerformanceView
              scope={scope}
              directory={directory}
              nowMs={nowMs}
              grades={gradeRows}
              updates={updateRows}
              loading={gradesPending}
              hasPanels={hasPanels}
            />
          ) : (
            <>
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
            </>
          )}
        </div>
      )}
      {view === "sources" && (
        <>
          <SourceCardV4
            source={opened?.source ?? null}
            space={opened?.space}
            nowMs={nowMs}
            onClose={() => setOpened(null)}
          />
          <AccountSettings />
        </>
      )}
    </Screen>
  );
}

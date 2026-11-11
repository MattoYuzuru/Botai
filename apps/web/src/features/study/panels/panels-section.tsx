import { AccentScope, Banner, Button, Icon, MonogramBadge, Skeleton } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { isApiError } from "@/api/errors";
import type { PanelOut } from "@/api/panels";
import { meQuery } from "@/api/queries";
import type { Scope } from "@/features/data/use-space-directory";
import { useCollapsedPanels } from "./collapsed";
import { groupBySpace, layoutPanels, panelsForScope } from "./model";
import { isRenderable, PanelView } from "./panel-view";
import { ProposedPanelCard } from "./proposed-card";
import { PanelSettingsSheet } from "./settings-sheet";
import { usePanelsData, useRefreshPanelsOnSync } from "./use-panels";

const SECTION_TITLE = "Успеваемость";

type Collapsed = ReturnType<typeof useCollapsedPanels>;

/**
 * Панели одного вуза. Ноутбук: показатели лентой без рамки сверху, под ней основная колонка (график, таблица,
 * список) и боковая (рейтинг, профиль) — 2/3 и 1/3; без боковых основная занимает всю ширину, без основных боковые
 * идут в три колонки. Телефон: одна колонка, плитки показателей по две в ряд.
 */
function PanelGrid({
  panels,
  zone,
  headingLevel,
  collapsed,
}: {
  panels: readonly PanelOut[];
  zone: string;
  headingLevel: 3 | 4;
  collapsed: Collapsed;
}) {
  const { band, main, side } = layoutPanels(panels);
  const view = (panel: PanelOut, bare = false, wide = false) => (
    <PanelView
      key={panel.id}
      panel={panel}
      context={{ zone, preview: false, wide }}
      headingLevel={headingLevel}
      collapsed={collapsed.isCollapsed(panel.id)}
      onCollapsedChange={(value) => collapsed.set(panel.id, value)}
      // Показатели идут лентой плиток: рамка вокруг рамок тяжелее, чем сами числа.
      bare={bare}
      // Заголовок раздела уже называет их «Успеваемость»: повторять его над плитками незачем.
      titleHidden={panel.spec.title.trim().toLowerCase() === SECTION_TITLE.toLowerCase()}
    />
  );
  // Последняя панель колонки растягивается до низа соседней: ряд заканчивается ровно, без пустоты под короче.
  const column = "flex min-w-0 flex-col gap-3 lg:gap-4 [&>*:last-child]:flex-1";
  return (
    <div className="flex min-w-0 flex-col gap-3 lg:gap-4">
      {band.map((panel) => view(panel, true))}
      {(main.length > 0 || side.length > 0) && (
        <div className="grid min-w-0 grid-cols-1 gap-3 lg:grid-cols-3 lg:gap-4">
          {main.length > 0 && (
            <div className={`${column} ${side.length > 0 ? "lg:col-span-2" : "lg:col-span-3"}`}>
              {main.map((panel) => view(panel, false, side.length > 0))}
            </div>
          )}
          {side.length > 0 && (
            <div className={main.length > 0 ? column : `${column} lg:col-span-3 lg:grid lg:grid-cols-3`}>
              {side.map((panel) => view(panel))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * «Успеваемость» (D-039): панели, которые описывает сервер, над карточками вузов и источников. Один вуз —
 * его панели сеткой; «Все вузы» — группы по вузам со знаком и акцентом, панели с `hiddenInAll` не показываются.
 * Нет панелей — блока нет (честнее пустой рамки: данных у вуза может ещё не быть).
 */
export function PanelsSection({
  scope,
  title = SECTION_TITLE,
  folded = false,
  onFoldedChange,
}: {
  scope: Scope;
  /**
   * Панели свёрнуты: видны заголовок, «Настроить» и «Показать панели». На телефоне «Успеваемость» сворачивает их,
   * когда выше уже есть «Предметы»: те же оценки не идут по экрану второй и третьей лентой.
   */
  folded?: boolean;
  /** Есть — под заголовком кнопка «Показать панели» / «Скрыть панели». */
  onFoldedChange?: (folded: boolean) => void;
  /**
   * Заголовок блока. Внутри «Успеваемости» (D-046) раздел уже так называется — там блок подписан «Панели»,
   * чтобы на экране не было двух «Успеваемостей».
   */
  title?: string;
}) {
  const me = useQuery(meQuery);
  const userId = me.data?.user.id ?? "anonymous";
  const collapsed = useCollapsedPanels(userId);
  const { active, proposed, loading, error } = usePanelsData();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const spaceIds = scope.spaces.map((space) => space.id);
  useRefreshPanelsOnSync(spaceIds);

  const zones = new Map(scope.spaces.map((space) => [space.id, space.timeZone]));
  const zoneOf = (spaceId: string) => zones.get(spaceId) ?? scope.timeZone;
  const grouped = scope.isAll && scope.spaces.length > 1;
  const inScope = { spaceIds, grouped };
  // Для настройки — все панели области, в том числе скрытые в «Все вузы» и выключенные.
  const configurable = panelsForScope(active ?? [], { spaceIds, grouped: false });
  const shown = panelsForScope(active ?? [], inScope).filter((panel) => panel.visible && isRenderable(panel));
  const suggestions = panelsForScope(proposed, { spaceIds, grouped: false });

  if (loading) {
    return (
      <section aria-label={title} aria-busy="true" data-panels="loading" className="flex flex-col gap-3">
        <Skeleton width="40%" className="h-6" />
        <Skeleton className="h-28 rounded-lg" />
      </section>
    );
  }
  // Сервер без панелей (движок v3, 404) — блока нет; настоящий сбой не прячем, но и не пугаем.
  if (error && !(isApiError(error) && error.status === 404)) {
    return (
      <Banner tone="info" icon="sync" live>
        Панели успеваемости не загрузились. Остальная «Учёба» доступна; панели появятся сами.
      </Banner>
    );
  }
  if (error || (configurable.length === 0 && suggestions.length === 0)) return null;

  // Сворачивать есть что — панели или предложения агента; иначе кнопки нет и прятать нечего.
  const foldable = Boolean(onFoldedChange) && shown.length + suggestions.length > 0;
  const isFolded = foldable && folded;
  const shownGroups = groupBySpace(shown);
  const suggestionGroups = groupBySpace(suggestions);
  const spacesInOrder = scope.spaces.flatMap((space) => {
    const own = shownGroups.find((group) => group.space.id === space.id);
    const proposals = suggestionGroups.find((group) => group.space.id === space.id);
    const panelSpace = own?.space ?? proposals?.space;
    return panelSpace
      ? [{ space: panelSpace, panels: own?.panels ?? [], proposals: proposals?.panels ?? [] }]
      : [];
  });
  const proposalCards = (proposals: readonly PanelOut[]) =>
    proposals.map((panel) => (
      <ProposedPanelCard key={panel.id} panel={panel} zone={zoneOf(panel.space.id)} />
    ));
  const headingLevel = grouped ? 4 : 3;

  return (
    <section
      aria-labelledby="study-panels-heading"
      data-panels="ready"
      className="flex min-w-0 flex-col gap-3"
    >
      <div className="flex items-center justify-between gap-2 px-1">
        <h2 id="study-panels-heading" className="type-heading text-fg">
          {title}
        </h2>
        {configurable.length > 0 && (
          <Button variant="ghost" leadingIcon="settings" onClick={() => setSettingsOpen(true)}>
            Настроить
          </Button>
        )}
      </div>

      {foldable && (
        <Button
          variant="secondary"
          className="w-full"
          aria-expanded={!isFolded}
          aria-controls="study-panels-body"
          onClick={() => onFoldedChange?.(!isFolded)}
        >
          {isFolded ? `Показать панели (${shown.length + suggestions.length})` : "Скрыть панели"}
          <Icon name={isFolded ? "chevron-down" : "chevron-up"} size={20} />
        </Button>
      )}

      {shown.length === 0 && configurable.length > 0 && (
        <p className="px-1 type-ui text-fg-muted">Все панели выключены. Включить их можно в настройках.</p>
      )}

      <div id="study-panels-body" hidden={isFolded} className="flex flex-col gap-6">
        {spacesInOrder.map(({ space, panels, proposals }) => (
          <AccentScope key={space.id} hue={space.accentHue}>
            <div
              {...(grouped ? { role: "group", "aria-labelledby": `study-panels-${space.id}` } : {})}
              data-panels-space={space.id}
              className="flex min-w-0 flex-col gap-3"
            >
              {grouped && (
                <h3
                  id={`study-panels-${space.id}`}
                  className="flex items-center gap-2 px-1 type-heading text-fg"
                >
                  <MonogramBadge
                    size="sm"
                    title={space.title}
                    shortTitle={space.shortTitle}
                    hue={space.accentHue}
                    decorative
                  />
                  {space.title}
                </h3>
              )}
              {panels.length > 0 && (
                <PanelGrid
                  panels={panels}
                  zone={zoneOf(space.id)}
                  headingLevel={headingLevel}
                  collapsed={collapsed}
                />
              )}
              {/* Предложения агента — после данных: сначала то, что уже есть, потом то, что можно добавить. */}
              {proposalCards(proposals)}
            </div>
          </AccentScope>
        ))}
      </div>

      <PanelSettingsSheet
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        panels={configurable}
        grouped={scope.spaces.length > 1}
      />
    </section>
  );
}

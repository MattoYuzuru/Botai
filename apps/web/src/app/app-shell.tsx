import { AccentScope, Banner, TabBar, useOnline, Wordmark } from "@botai/design";
import { useIsFetching, useQuery } from "@tanstack/react-query";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { meQuery, spacesQuery } from "@/api/queries";
import { MascotSyncIndicator } from "@/features/mascot/mascot-sync-indicator";
import { ActiveJobBar } from "./active-job-bar";
import { useActiveSpaceId } from "./active-space";
import { useComposerKeyboard } from "./keyboard";
import { activeTab, MAIN_TABS, type TabPath } from "./nav";

/**
 * Каркас приложения: пять вкладок (нижняя панель до 1024 px, боковая 240 px от 1024 px), акцент активного
 * пространства, `100dvh`, safe area. Открытая клавиатура над полем чата прячет нижнюю панель.
 */
export function AppShell() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const spaces = useQuery(spacesQuery);
  const me = useQuery(meQuery);
  const activeSpaceId = useActiveSpaceId(spaces.data);
  const hue = spaces.data?.find((space) => space.id === activeSpaceId)?.accentHue ?? null;
  const keyboardOpen = useComposerKeyboard();
  const online = useOnline();
  const refreshing = useIsFetching({
    predicate: (query) => query.state.fetchStatus === "fetching" && query.state.dataUpdateCount > 0,
  });

  return (
    <AccentScope hue={hue} className="min-h-dvh bg-bg text-fg">
      <TabBar
        items={MAIN_TABS}
        activeId={activeTab(pathname) ?? ""}
        hidden={keyboardOpen}
        brand={<Wordmark size="md" />}
        footer={
          <div className="flex items-center justify-between gap-2 px-3 type-caption text-fg-muted">
            <span className="min-w-0 truncate">{me.data?.user.displayName ?? ""}</span>
            <MascotSyncIndicator state={refreshing > 0 ? "syncing" : "idle"} />
          </div>
        }
        renderLink={(item, props) => (
          <Link to={item.href as TabPath} {...props}>
            {props.children}
          </Link>
        )}
      />
      <div className="flex min-h-dvh flex-col lg:pl-rail">
        {!online && (
          <Banner tone="info" icon="offline" live className="rounded-none px-safe-4 pt-safe-3">
            Нет сети. Показаны последние данные, они обновятся, когда сеть вернётся.
          </Banner>
        )}
        <ActiveJobBar />
        <Outlet />
      </div>
    </AccentScope>
  );
}

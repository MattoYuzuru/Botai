import { MascotAnimationPolicy } from "@botai/design";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { WallMascot } from "@/features/connect/wall-mascot";
import type { Scope, SpaceDirectory, SpaceInfo } from "@/features/data/use-space-directory";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager } from "@/features/jobs/job-manager";
import { DemoJobManager, SHOWCASE_JOBS } from "@/features/styleguide/fixtures-agent";
import type { Scenario } from "@/mocks/db";
import { createDataServer, NOW } from "@/test/data-server";
import { ProgressWall } from "@/widgets/progress-wall/progress-wall";
import { MascotPresence } from "./mascot-presence";
import { MascotSyncIndicator } from "./mascot-sync-indicator";
import { availabilityOf } from "./use-mascot";

let current = createDataServer();
function setScenario(scenario: Scenario) {
  current.server.close();
  current = createDataServer(scenario);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => setScenario("connected"));
afterEach(() => {
  current.server.resetHandlers();
  // «Войти снова» привязывает задание ко всему приложению: следующий тест начинается без него.
  useActiveJob.getState().clear();
  jobManager.stopAll();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const wait = { timeout: 8000 };

describe("ProgressWall: Варя на рельсе", () => {
  function wall(key: keyof typeof SHOWCASE_JOBS) {
    const manager = new DemoJobManager();
    manager.seed(SHOWCASE_JOBS[key]);
    return render(
      <MascotAnimationPolicy exclusive={false}>
        <ProgressWall
          jobId={SHOWCASE_JOBS[key].jobId}
          title="Подключаю ВШЭ"
          manager={manager}
          onMinimize={() => {}}
          onFinish={() => {}}
          renderMascot={(state, slot) => <WallMascot state={state} slot={slot} />}
        />
      </MascotAnimationPolicy>,
    );
  }

  it("работа идёт: Варя бежит по рельсу на месте, клюв вправо; процента и срока нет", () => {
    const { container } = wall("reconnecting");
    const runner = container.querySelector("[data-rail-runner] svg.varya");
    expect(runner).not.toBeNull();
    expect(runner?.getAttribute("data-sit")).toBe("connect");
    expect(runner?.classList.contains("still")).toBe(false);
    expect(container.querySelector("[data-rail-runner] [data-mascot]")?.className).toContain("-scale-x-100");
    expect(screen.getByText("Сколько осталось — пока неизвестно")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\d\s*%/);
  });

  it("агент ждёт ответа: Варя в позе ожидания и стоит; вопрос и «Жду ответа» остаются текстом", () => {
    const { container } = wall("question");
    const runner = container.querySelector("[data-rail-runner] svg.varya");
    expect(runner?.getAttribute("data-sit")).toBe("tip");
    expect(runner?.classList.contains("still")).toBe(true);
    expect(screen.getByText("Нужен ответ")).toBeInTheDocument();
    expect(screen.getAllByText("Жду ответа").length).toBeGreaterThan(0);
  });

  it("итог: рельса нет, Варя рядом с названием в позе «сдано», статично", () => {
    const { container } = wall("done");
    expect(container.querySelector("[data-rail-runner]")).toBeNull();
    const header = container.querySelector("header svg.varya");
    expect(header?.getAttribute("data-sit")).toBe("done");
    expect(header?.classList.contains("still")).toBe(true);
    expect(screen.getByRole("button", { name: "Готово" })).toBeInTheDocument();
  });

  it("сбой: поза «не получилось», сам сбой — полосой с текстом", () => {
    const { container } = wall("failed");
    expect(container.querySelector("header svg.varya")?.getAttribute("data-sit")).toBe("error");
    expect(screen.getByText("Подключение не завершено.")).toBeInTheDocument();
  });

  it("без функции Вари экран прежний: картинки нет", () => {
    const manager = new DemoJobManager();
    manager.seed(SHOWCASE_JOBS.reconnecting);
    const { container } = render(
      <ProgressWall
        jobId={SHOWCASE_JOBS.reconnecting.jobId}
        title="Подключаю ВШЭ"
        manager={manager}
        onMinimize={() => {}}
        onFinish={() => {}}
      />,
    );
    expect(container.querySelector("svg.varya")).toBeNull();
  });
});

describe("состояние Вари из состояния источников", () => {
  const space = (state: SpaceInfo["health"]["state"]): SpaceInfo => ({
    id: state,
    title: state,
    shortTitle: state,
    timeZone: "Europe/Moscow",
    hue: 262,
    universityId: null,
    connections: [],
    health: { state, updatedAt: null },
  });
  const directory = (spaces: SpaceInfo[], extra: Partial<SpaceDirectory> = {}): SpaceDirectory => ({
    spaces,
    byId: new Map(spaces.map((item) => [item.id, item])),
    loaded: true,
    connectionsLoaded: true,
    none: spaces.length === 0,
    homeZone: "Europe/Moscow",
    ...extra,
  });
  const scope = (spaces: SpaceInfo[]): Scope => ({
    activeId: "all",
    isAll: true,
    spaces,
    spaceId: undefined,
    timeZone: "Europe/Moscow",
  });
  const run = (
    states: SpaceInfo["health"]["state"][],
    online = true,
    extra: Partial<SpaceDirectory> = {},
  ) => {
    const spaces = states.map(space);
    return availabilityOf(directory(spaces, extra), scope(spaces), online);
  };

  it("приоритет: нет сети, вход, починка; всё хорошо — ready", () => {
    expect(run(["ok", "reauth"], false)).toBe("offline");
    expect(run(["ok", "offline"])).toBe("offline");
    expect(run(["ok", "reauth", "repairing"])).toBe("reauth");
    expect(run(["ok", "repairing"])).toBe("repairing");
    expect(run(["ok", "stale"])).toBe("ready");
    expect(run(["syncing"])).toBe("ready");
  });

  it("вузов нет или подключений нет — «empty» (приглашение подключить, а не «всё сдано»); список не загружен — unknown", () => {
    expect(run([])).toBe("empty");
    expect(run(["none", "none"])).toBe("empty");
    expect(run(["none", "ok"])).toBe("ready");
    expect(run(["ok"], true, { loaded: false })).toBe("unknown");
  });
});

describe("MascotPresence: тихое присутствие и действия", () => {
  function mount(screenName: "today" | "schedule", scenario: Scenario = "connected") {
    setScenario(scenario);
    const root = createRootRoute({
      component: () => (
        <>
          <MascotPresence screen={screenName === "today" ? "other" : "schedule"} />
          <Outlet />
        </>
      ),
    });
    const paths = ["/", "/tasks", "/schedule", "/chat", "/connect", "/study"];
    const children = paths.map((path) =>
      createRoute({
        getParentRoute: () => root,
        path,
        component: () => <p data-page={path}>страница {path}</p>,
      }),
    );
    const router = createRouter({
      routeTree: root.addChildren(children),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return {
      router,
      ...render(
        <QueryClientProvider client={client}>
          <RouterProvider router={router} />
        </QueryClientProvider>,
      ),
    };
  }

  it("срочная работа: подпись, бейдж и кнопка «Открыть срочную» ведёт в «Задания»", async () => {
    const user = userEvent.setup();
    const { router } = mount("today");
    const button = await screen.findByRole("button", { name: /Открыть (срочную|работу)/ }, wait);
    // Смысл дублируется текстом: бейдж и реплика рядом с картинкой.
    expect(document.querySelector("[data-mascot-badge]")).not.toBeNull();
    await user.click(button);
    await waitFor(() => expect(router.state.location.pathname).toBe("/tasks"));
  });

  it("нужен вход: «Войти снова» запускает вход по подключению, а не «повторяет запрос»", async () => {
    const user = userEvent.setup();
    const requests: string[] = [];
    const { container } = mount("today", "reauth");
    current.server.events.on("request:start", ({ request }) => {
      if (request.method === "POST") requests.push(new URL(request.url).pathname);
    });
    const button = await screen.findByRole("button", { name: "Войти снова" }, wait);
    expect(container.querySelector("svg.varya")?.getAttribute("data-sit")).toBe("reauth");
    await user.click(button);
    await waitFor(
      () => expect(requests.some((path) => /\/connections\/[^/]+\/sync$/.test(path))).toBe(true),
      wait,
    );
  });

  it("нет данных: приглашение «Подключить вуз» ведёт на подключение", async () => {
    const user = userEvent.setup();
    const { router } = mount("today", "empty");
    await user.click(await screen.findByRole("button", { name: "Подключить вуз" }, wait));
    await waitFor(() => expect(router.state.location.pathname).toBe("/connect"));
  });

  it("в покое Варя не показывается: ни картинки, ни подписи", async () => {
    setScenario("fresh");
    const { container } = mount("today", "fresh");
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(container.querySelector("svg.varya")).toBeNull();
    expect(container.querySelector("[data-mascot-stage]")).toBeNull();
  });
});

describe("MascotSyncIndicator", () => {
  it("Варя 20 px вместо точки, пока идёт обновление; в покое ничего, итог объявляется один раз", () => {
    const { container, rerender } = render(<MascotSyncIndicator state="syncing" />);
    expect(container.querySelector("[data-sync-mascot] svg.varya")).not.toBeNull();
    expect(container.querySelector("[data-sync-dot]")).toBeNull();
    expect(container.querySelector("[data-sync-mascot]")).toHaveAttribute("aria-hidden", "true");
    rerender(<MascotSyncIndicator state="idle" />);
    expect(container.querySelector("svg.varya")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Данные обновлены");
  });
});

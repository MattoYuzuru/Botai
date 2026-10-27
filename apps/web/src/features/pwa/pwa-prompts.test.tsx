import { toast } from "@botai/design";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";
import { usePwaStore } from "./register";

let current = createDataServer();

const IPHONE =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  current.server.listen({ onUnhandledRequest: "error" });
});
beforeEach(() => {
  current.server.close();
  current = createDataServer("connected");
  current.server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
  usePwaStore.setState({ waiting: null });
  vi.restoreAllMocks();
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

describe("плашка «Доступна новая версия»", () => {
  it("видна, когда версия ждёт; «Перезапустить» просит ждущую версию стать рабочей", async () => {
    const posted: unknown[] = [];
    const waiting = { postMessage: (message: unknown) => posted.push(message) } as unknown as ServiceWorker;
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    expect(screen.queryByText("Доступна новая версия")).toBeNull();
    usePwaStore.setState({ waiting });
    expect(await screen.findByText("Доступна новая версия")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Перезапустить" }));
    expect(posted).toEqual([{ type: "SKIP_WAITING" }]);
    // Это не «Повторить/Обновить»: действие — смена версии по решению человека.
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
  });

  it("не мешает поверх нижней панели: слой плашки выше панели и тоста", async () => {
    usePwaStore.setState({ waiting: { postMessage: () => undefined } as unknown as ServiceWorker });
    renderApp("/");
    const banner = await waitFor(() => {
      const node = document.querySelector("[data-pwa-update]");
      if (!node) throw new Error("нет плашки");
      return node;
    });
    expect(banner.className).toContain("z-banner");
    expect(banner.className).toContain("bottom-safe");
  });
});

describe("подсказка «Добавить на экран „Домой“»", () => {
  it("на ноутбуке и Android не показывается; на iPhone до входа — тоже", async () => {
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText("Добавьте BotAI на экран «Домой»")).toBeNull();
  });

  it("уже установленное приложение подсказки не получает", async () => {
    vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue(IPHONE);
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query: string) =>
        ({
          matches: query.includes("display-mode: standalone"),
          media: query,
          addEventListener: () => undefined,
          removeEventListener: () => undefined,
        }) as unknown as MediaQueryList,
    );
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText("Добавьте BotAI на экран «Домой»")).toBeNull();
  });
  it("на iPhone показывается один раз и не возвращается при следующем открытии", async () => {
    vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue(IPHONE);
    const first = renderApp("/");
    expect(await screen.findByText("Добавьте BotAI на экран «Домой»")).toBeInTheDocument();
    expect(screen.getByText(/Нажмите «Поделиться» в Safari/)).toBeInTheDocument();
    expect(window.localStorage.getItem("botai.install-hint")).not.toBeNull();
    first.unmount();
    toast.dismiss();
    await waitFor(() => expect(screen.queryByText("Добавьте BotAI на экран «Домой»")).toBeNull(), {
      timeout: 4000,
    });
    renderApp("/schedule");
    await screen.findByRole("heading", { level: 1, name: "Расписание" });
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByText("Добавьте BotAI на экран «Домой»")).toBeNull();
  });
});

describe("снимки и выход", () => {
  it("выход стирает офлайн-снимки service worker вместе с кэшем в памяти", async () => {
    const del = vi.fn(async () => true);
    vi.stubGlobal("caches", { delete: del });
    renderApp("/study");
    await userEvent.click(await screen.findByRole("button", { name: "Выйти из BotAI" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("botai-api-v1"));
    vi.unstubAllGlobals();
  });

  it("при смене человека на устройстве снимки прежнего стираются", async () => {
    const del = vi.fn(async () => true);
    vi.stubGlobal("caches", { delete: del });
    window.localStorage.setItem("botai.snapshot-owner", "someone-else");
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    await waitFor(() => expect(del).toHaveBeenCalledWith("botai-api-v1"));
    expect(window.localStorage.getItem("botai.snapshot-owner")).toBe(current.db.users[0]?.id);
    vi.unstubAllGlobals();
  });
});

describe("подогрев снимков", () => {
  const requested = () => {
    const urls: string[] = [];
    current.server.events.on("request:start", ({ request }) => urls.push(new URL(request.url).pathname));
    return urls;
  };

  it("с service worker «Сегодня» заодно запрашивает расписание недели и задания: вкладки откроются без сети", async () => {
    Object.defineProperty(navigator, "serviceWorker", { value: { controller: {} }, configurable: true });
    const urls = requested();
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    await waitFor(() => {
      expect(urls).toContain("/api/v1/schedule");
      expect(urls).toContain("/api/v1/deadlines");
    });
    Reflect.deleteProperty(navigator, "serviceWorker");
  });

  it("без service worker (dev, тесты) лишних запросов нет", async () => {
    const urls = requested();
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    await waitFor(() => expect(urls).toContain("/api/v1/today"));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(urls).not.toContain("/api/v1/schedule");
    expect(urls).not.toContain("/api/v1/deadlines");
  });

  it("выбран один вуз — греется и его область: у запросов с `space_id` другие адреса", async () => {
    Object.defineProperty(navigator, "serviceWorker", { value: { controller: {} }, configurable: true });
    const spaceId = (current.db.spaces[1] as { id: string }).id;
    useActiveSpaceStore.setState({ id: spaceId });
    const urls: string[] = [];
    current.server.events.on("request:start", ({ request }) => urls.push(request.url));
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    await waitFor(() => {
      expect(urls.some((url) => url.includes("/api/v1/schedule") && !url.includes("space_id"))).toBe(true);
      expect(
        urls.some((url) => url.includes("/api/v1/schedule") && url.includes(`space_id=${spaceId}`)),
      ).toBe(true);
      expect(
        urls.some((url) => url.includes("/api/v1/deadlines") && url.includes(`space_id=${spaceId}`)),
      ).toBe(true);
    });
    useActiveSpaceStore.setState({ id: "all" });
    Reflect.deleteProperty(navigator, "serviceWorker");
  });

  it("вернулась сеть — данные запрашиваются заново сразу, а не по таймеру (если работает service worker)", async () => {
    Object.defineProperty(navigator, "serviceWorker", { value: { controller: {} }, configurable: true });
    const urls: string[] = [];
    current.server.events.on("request:start", ({ request }) => urls.push(new URL(request.url).pathname));
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    await waitFor(() => expect(urls).toContain("/api/v1/me"));
    const before = urls.filter((path) => path === "/api/v1/me").length;
    window.dispatchEvent(new Event("online"));
    await waitFor(() => expect(urls.filter((path) => path === "/api/v1/me").length).toBeGreaterThan(before));
    Reflect.deleteProperty(navigator, "serviceWorker");
  });
});

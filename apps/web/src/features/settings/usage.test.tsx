import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { formatTokens } from "@/lib/money";
import { createDataServer, NOW } from "@/test/data-server";
import { UsageSection } from "./usage";

let current = createDataServer("connected");

function useServer() {
  current.server.close();
  current = createDataServer("connected");
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => useServer());
afterEach(() => {
  current.server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

function makeOwner() {
  const user = current.db.users[0];
  if (!user) throw new Error("в моке нет пользователя");
  user.isOwner = true;
}

function renderUsage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <UsageSection />
    </QueryClientProvider>,
  );
}

const problem = (status: number, code: string, detail: string) =>
  new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );

const card = () => screen.findByRole("region", { name: "Расход и лимиты" });

describe("расход и лимиты", () => {
  it("сегодня: токены из лимита и время сброса, только настоящие числа", async () => {
    const usage = renderUsage();
    const region = await card();
    expect(await within(region).findByText("41 250 токенов из 1 000 000 токенов")).toBeInTheDocument();
    expect(within(region).getByText(/Лимит сбросится 27 октября/)).toBeInTheDocument();
    expect(within(region).getByText(/Стоимость сегодня:/)).toBeInTheDocument();
    // Процентов и придуманного прогноза нет.
    expect(usage.container.textContent).not.toMatch(/\d+\s*%/);
  });

  it("последние дни: список с настоящими токенами и стоимостью и итогом за период", async () => {
    const usage = renderUsage();
    const region = await card();
    const days = await within(region).findByRole("list", { name: "Расход по дням" });
    expect(within(days).getAllByRole("listitem")).toHaveLength(7);
    expect(within(days).getByText("сегодня")).toBeInTheDocument();
    expect(within(region).getByText(/За 7 дней/)).toBeInTheDocument();
    expect(usage.container.textContent).toContain(formatTokens(183_250));
  });

  it("пусто: «за последние дни расхода не было», без выдуманных нулей-процентов", async () => {
    current.server.use(
      http.get("*/api/v1/usage", () =>
        HttpResponse.json({
          tokensToday: 0,
          tokensLimit: 1_000_000,
          resetsAt: new Date(NOW + 6 * 3_600_000).toISOString(),
          lastDays: [],
        }),
      ),
    );
    renderUsage();
    const region = await card();
    expect(await within(region).findByText(/За последние дни расхода не было/)).toBeInTheDocument();
    expect(within(region).queryByRole("list", { name: "Расход по дням" })).toBeNull();
    expect(region.textContent).not.toMatch(/\d+\s*%/);
  });

  it("ошибка загрузки видна и не выдаётся за ноль расхода", async () => {
    current.server.use(
      http.get("*/api/v1/usage", () => problem(500, "internal_error", "Учёт расхода временно недоступен")),
    );
    renderUsage();
    const region = await card();
    expect(await within(region).findByText(/Расход не загрузился/)).toBeInTheDocument();
    expect(within(region).queryByText(/За последние дни расхода не было/)).toBeNull();
  });

  it("не владелец: блок общего ключа не показывается и не запрашивается", async () => {
    renderUsage();
    await card();
    expect(screen.queryByText("Общий ключ")).toBeNull();
  });

  it("владелец: расход общего ключа по людям и итог", async () => {
    makeOwner();
    renderUsage();
    const region = await card();
    expect(await within(region).findByText("Общий ключ")).toBeInTheDocument();
    const users = await within(region).findByRole("list", { name: "Расход по людям" });
    expect(within(users).getAllByRole("listitem")).toHaveLength(2);
    expect(within(users).getByText("Мария")).toBeInTheDocument();
    expect(within(region).getByText(/Всего сегодня:/)).toBeInTheDocument();
    expect(within(region).getByText(/лимит 1 000 000 токенов в день/)).toBeInTheDocument();
  });

  it("владелец, но сервер отказал: честный текст про доступ", async () => {
    makeOwner();
    current.server.use(http.get("*/api/v1/usage/pool", () => problem(403, "forbidden", "Только владелец")));
    renderUsage();
    const region = await card();
    expect(await within(region).findByText("Расход общего ключа видит только владелец.")).toBeInTheDocument();
  });

  it("a11y: карточка — именованный регион, списки с именами", async () => {
    renderUsage();
    const region = await card();
    expect(await within(region).findByRole("list", { name: "Расход по дням" })).toBeInTheDocument();
    expect(within(region).getByRole("heading", { level: 2, name: "Расход и лимиты" })).toBeInTheDocument();
  });
});

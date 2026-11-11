import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { MockPolicy } from "@/mocks/db";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

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
  window.localStorage.clear();
  useActiveSpaceStore.setState({ id: "all" });
  vi.restoreAllMocks();
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const problem = (status: number, code: string, detail: string) =>
  new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );

const hseId = () => (current.db.spaces[0] as { id: string }).id;

async function openSourceCard() {
  const user = userEvent.setup();
  renderApp("/study?view=sources");
  const panel = await screen.findByRole("region", { name: /^Высшая школа экономики/ });
  await user.click(await within(panel).findByRole("button", { name: /^Личный кабинет:/ }));
  return { user, card: await screen.findByRole("dialog", { name: "Личный кабинет" }) };
}

describe("автономия в карточке источника", () => {
  it("по умолчанию: все классы подписаны, чтение — автоматически, остальное спрашивает", async () => {
    const { card } = await openSourceCard();
    const autonomy = await within(card).findByRole("region", { name: "Автономия" });
    const rows = autonomy.querySelectorAll("[data-policy-class]");
    expect(rows).toHaveLength(5);
    expect(within(autonomy).getAllByText("по умолчанию")).toHaveLength(5);
    expect(within(autonomy).getByLabelText("Чтение: режим")).toHaveTextContent("Автоматически");
    expect(within(autonomy).getByLabelText("Изменения: режим")).toHaveTextContent("Спрашивать");
    expect(within(autonomy).getByText(/источника\s+важнее настройки вуза/)).toBeInTheDocument();
  });

  it("смена режима создаёт свою строку политики, сброс возвращает дефолт", async () => {
    const { user, card } = await openSourceCard();
    const autonomy = await within(card).findByRole("region", { name: "Автономия" });
    await user.click(within(autonomy).getByLabelText("Изменения: режим"));
    const sheet = await screen.findByRole("dialog", { name: "Изменения: режим" });
    await user.click(within(sheet).getByRole("option", { name: "Автоматически" }));

    await waitFor(() =>
      expect(current.db.actionPolicies).toContainEqual(
        expect.objectContaining({
          scopeKind: "source",
          scopeId: "src-hse-portal",
          actionClass: "write_soft",
          mode: "auto",
        }),
      ),
    );
    expect(await within(autonomy).findByText("своя настройка")).toBeInTheDocument();
    expect(within(autonomy).getByLabelText("Изменения: режим")).toHaveTextContent("Автоматически");

    await user.click(within(autonomy).getByRole("button", { name: "Вернуть по умолчанию: Изменения" }));
    await waitFor(() => expect(current.db.actionPolicies).toHaveLength(0));
    await waitFor(() =>
      expect(within(autonomy).getByLabelText("Изменения: режим")).toHaveTextContent("Спрашивать"),
    );
  });

  it("наследование от общей настройки подписано, а не выдаётся за свою", async () => {
    const globalPolicy: MockPolicy = {
      id: "pol-global",
      scopeKind: "global",
      scopeId: null,
      actionClass: "money",
      mode: "never",
      limits: {},
      note: null,
      updatedAt: new Date(NOW).toISOString(),
    };
    current.db.actionPolicies.push(globalPolicy);
    const { card } = await openSourceCard();
    const autonomy = await within(card).findByRole("region", { name: "Автономия" });
    const moneyRow = autonomy.querySelector('[data-policy-class="money"]') as HTMLElement;
    expect(within(moneyRow).getByText("из общих настроек")).toBeInTheDocument();
    expect(within(moneyRow).getByLabelText("Деньги: режим")).toHaveTextContent("Никогда");
    expect(within(moneyRow).queryByRole("button", { name: /Вернуть по умолчанию/ })).toBeNull();
  });

  it("из карточки можно открыть журнал, отфильтрованный по источнику", async () => {
    const { card } = await openSourceCard();
    const link = await within(card).findByRole("link", { name: "Журнал действий" });
    expect(link).toHaveAttribute("href", "/actions?source=src-hse-portal");
    expect(hseId()).toBeTruthy();
  });

  it("общие настройки автономии есть на экране «Учёба» внизу", async () => {
    renderApp("/study?view=sources");
    expect(await screen.findByRole("region", { name: "Автономия" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть журнал действий" })).toHaveAttribute("href", "/actions");
  });
});

describe("журнал действий", () => {
  const journal = () => screen.findByRole("list", { name: "Журнал действий" });

  it("список: предложения, решения, классы и время", async () => {
    renderApp("/actions");
    const list = await journal();
    await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(5));
    expect(within(list).getByText("Записаться на пересдачу по алгоритмам")).toBeInTheDocument();
    expect(within(list).getByText("Ждёт решения")).toBeInTheDocument();
    // Область подписана названием источника, если сервер его дал.
    expect(within(list).getAllByText("Личный кабинет").length).toBeGreaterThan(0);
    expect(within(list).getByText("все источники")).toBeInTheDocument();
    expect(within(list).getByText("Выполнено")).toBeInTheDocument();
    expect(within(list).getByText("Запрещено владельцем")).toBeInTheDocument();
    expect(within(list).getByText("Не выполнено")).toBeInTheDocument();
    expect(screen.getByText(/Политику автономии можно изменить/)).toBeInTheDocument();
  });

  it("фильтр «ждут решения» оставляет только спрошенные действия", async () => {
    const user = userEvent.setup();
    renderApp("/actions");
    await journal();
    await user.click(screen.getByLabelText("Решение"));
    const sheet = await screen.findByRole("dialog", { name: "Решение" });
    await user.click(within(sheet).getByRole("option", { name: "Ждут решения" }));
    await waitFor(async () => {
      const list = await journal();
      expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    });
  });

  it("карточка записи: предложение, поля, запрос и ссылка на трассу", async () => {
    const user = userEvent.setup();
    renderApp("/actions");
    const list = await journal();
    await user.click(within(list).getByRole("button", { name: /^Записаться на пересдачу/ }));
    const sheet = await screen.findByRole("dialog", { name: "Записаться на пересдачу по алгоритмам" });
    expect(within(sheet).getByText("Метод и адрес")).toBeInTheDocument();
    expect(within(sheet).getByText(/POST https:\/\/lk\.fakeuni\.test\/api\/requests/)).toBeInTheDocument();
    expect(within(sheet).getByRole("list", { name: "Поля предложения" })).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Открыть трассу хода" })).toHaveAttribute(
      "href",
      "/agent/runs/run-answer",
    );
  });

  it("«Разрешить» записывает решение и обновляет запись, но не отправляет запрос", async () => {
    const user = userEvent.setup();
    renderApp("/actions");
    const list = await journal();
    await user.click(within(list).getByRole("button", { name: /^Записаться на пересдачу/ }));
    const sheet = await screen.findByRole("dialog", { name: "Записаться на пересдачу по алгоритмам" });
    expect(within(sheet).getByText(/Решение запишется в журнал/)).toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: "Разрешить" }));
    await waitFor(() => expect(current.db.actionsLog[0]?.decision).toBe("approved"));
    expect(await within(sheet).findByText("Разрешено")).toBeInTheDocument();
    expect(within(sheet).queryByRole("button", { name: "Разрешить" })).toBeNull();
  });

  it("«Запретить» просит подтверждения и закрывает действие", async () => {
    const user = userEvent.setup();
    renderApp("/actions");
    const list = await journal();
    await user.click(within(list).getByRole("button", { name: /^Записаться на пересдачу/ }));
    const sheet = await screen.findByRole("dialog", { name: "Записаться на пересдачу по алгоритмам" });
    await user.click(within(sheet).getByRole("button", { name: "Запретить" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Запретить действие?" });
    await user.click(within(dialog).getByRole("button", { name: "Запретить" }));
    await waitFor(() => expect(current.db.actionsLog[0]?.decision).toBe("denied"));
    expect(await within(sheet).findByText("Запрещено владельцем")).toBeInTheDocument();
  });

  it("фильтр по источнику из журнала: список сужается до действий этого источника", async () => {
    const user = userEvent.setup();
    renderApp("/actions");
    const list = await journal();
    await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(5));
    await user.click(screen.getByLabelText("Источник"));
    const sheet = await screen.findByRole("dialog", { name: "Источник" });
    await user.click(within(sheet).getByRole("option", { name: "Личный кабинет" }));
    await waitFor(async () => {
      const filtered = await journal();
      expect(within(filtered).getAllByRole("listitem")).toHaveLength(4);
      expect(within(filtered).queryByText("Купить полный доступ к курсу")).toBeNull();
    });
  });

  it("журнал по источнику: фильтр из адреса и сброс к общему журналу", async () => {
    const user = userEvent.setup();
    renderApp("/actions?source=src-hse-portal");
    const list = await journal();
    await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(4));
    expect(within(list).queryByText("Купить полный доступ к курсу")).toBeNull();
    expect(screen.getByLabelText("Источник")).toHaveTextContent("Личный кабинет");
    await user.click(screen.getByLabelText("Источник"));
    const sheet = await screen.findByRole("dialog", { name: "Источник" });
    await user.click(within(sheet).getByRole("option", { name: "Все источники" }));
    await waitFor(async () => {
      const all = await journal();
      expect(within(all).getAllByRole("listitem")).toHaveLength(5);
    });
  });

  it("пусто: «Действий пока нет», без выдуманных записей", async () => {
    current.server.use(http.get("*/api/v1/actions", () => HttpResponse.json([])));
    renderApp("/actions");
    expect(await screen.findByText("Действий пока нет")).toBeInTheDocument();
    expect(document.querySelector('[data-screen="actions"]')).toHaveAttribute("data-state", "empty");
  });

  it("ошибка загрузки видна, а не выдаётся за пустой журнал", async () => {
    current.server.use(
      http.get("*/api/v1/actions", () => problem(500, "internal_error", "Журнал временно недоступен")),
    );
    renderApp("/actions");
    expect(await screen.findByText(/Журнал не загрузился/)).toBeInTheDocument();
    expect(screen.queryByText("Действий пока нет")).toBeNull();
  });

  it("a11y: один h1, список с именем, решение — доступные кнопки", async () => {
    renderApp("/actions");
    const list = await journal();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(list.tagName).toBe("UL");
    expect(within(list).getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByLabelText("Решение")).toBeInTheDocument();
    expect(screen.getByLabelText("Класс действия")).toBeInTheDocument();
  });
});

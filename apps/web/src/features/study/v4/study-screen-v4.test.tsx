import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { SourceV4 } from "@/api/sources-v4";
import { useActiveSpaceStore } from "@/app/active-space";
import { hrefOf } from "@/test/data-fixtures";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

/** Флаг нового UI: тесты включают его через localStorage, как живая приёмка через `?studyUi=v4`. */
const FLAG = "botai.study.v4";

const current = createDataServer();

const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString();

function v4Source(spaceId: string, patch: Partial<SourceV4> & Pick<SourceV4, "id" | "title">): SourceV4 {
  return {
    spaceId,
    universityId: null,
    entryUrl: null,
    kinds: ["portal"],
    status: "active",
    playbookId: null,
    config: {},
    authRef: null,
    priority: 0,
    trust: "normal",
    lastSyncAt: iso(5),
    lastError: null,
    disabledFrom: null,
    createdAt: iso(60 * 24),
    updatedAt: iso(5),
    ...patch,
  };
}

const spaceId = (index: number) => (current.db.spaces[index] as { id: string }).id;

/** Ответ v4-API для обоих вузов мока; тест может подменить его своим обработчиком. */
function installSources(sources: (spaceId: string | null) => SourceV4[]) {
  current.server.use(
    http.get("*/api/v1/sources", ({ request }) => {
      const query = new URL(request.url).searchParams.get("space_id");
      return HttpResponse.json(sources(query));
    }),
  );
}

function connectedSources(space: string | null): SourceV4[] {
  const hse = spaceId(0);
  const cu = spaceId(1);
  const bySpace: Record<string, SourceV4[]> = {
    [hse]: [
      v4Source(hse, {
        id: "src-hse-portal",
        title: "Личный кабинет",
        kinds: ["portal", "schedule"],
        entryUrl: "https://lk.fakeuni.test/",
        playbookId: "pb-1",
      }),
      v4Source(hse, {
        id: "src-hse-lms",
        title: "Smart LMS",
        kinds: ["lms", "grades"],
        status: "needs_reauth",
        lastSyncAt: iso(60 * 10),
        lastError: { code: "source_auth_required", detail: "Сессия источника протухла" },
      }),
    ],
    [cu]: [
      v4Source(cu, {
        id: "src-cu-portal",
        title: "Личный кабинет ЦУ",
        status: "error",
        lastSyncAt: null,
        lastError: { code: "recipe_drift", detail: "Сайт вуза изменил разметку" },
      }),
    ],
  };
  if (space) return bySpace[space] ?? [];
  return Object.values(bySpace).flat();
}

function notFoundV4() {
  return new HttpResponse(
    JSON.stringify({
      type: "https://botai.mnema.app/errors/not_found",
      title: "not_found",
      status: 404,
      code: "not_found",
      detail: "Источники v4 выключены: включите BOTAI_AGENT_ENGINE=v4",
    }),
    { status: 404, headers: { "content-type": "application/problem+json" } },
  );
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  current.server.listen({ onUnhandledRequest: "error" });
});

beforeEach(() => {
  window.localStorage.setItem(FLAG, "1");
});

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

const studyPanel = (name: RegExp) => screen.findByRole("region", { name });

describe("«Учёба» v4: источники вуза", () => {
  it("включается флагом: вузы с источниками из нового API и документами в карточке вуза", async () => {
    installSources(connectedSources);
    renderApp("/study");

    expect(await screen.findByRole("heading", { level: 1, name: "Учёба" })).toBeInTheDocument();
    const hse = await studyPanel(/^Высшая школа экономики/);
    const sources = await within(hse).findByRole("list", { name: "Источники: ВШЭ" });
    await waitFor(() => expect(within(sources).getAllByRole("listitem")).toHaveLength(2));
    expect(within(sources).getByText("Личный кабинет")).toBeInTheDocument();
    expect(within(sources).getByText("Smart LMS")).toBeInTheDocument();
    // Статус — словами и иконкой, а не только цветом; время последней синхронизации видно.
    expect(within(sources).getAllByText("Работает").length).toBeGreaterThan(0);
    expect(within(sources).getByText("Нужен вход")).toBeInTheDocument();
    expect(within(sources).getAllByText(/Обновлено/).length).toBeGreaterThan(0);
    // Виды источника — подписями, природа — иконкой строки.
    expect(within(sources).getByText("Расписание")).toBeInTheDocument();

    // «Добавить источник» и «Спросить агента» ведут в тот же агентский тред с готовой фразой.
    const add = within(hse).getByRole("link", { name: "Добавить источник" });
    expect(hrefOf(add).pathname).toBe("/chat");
    expect(hrefOf(add).draft).toContain("Высшая школа экономики");
    expect(within(hse).getByRole("link", { name: "Спросить агента" })).toBeInTheDocument();

    // Документы — в карточке вуза, без отдельного раздела «Документы и вложения».
    expect(within(hse).getByText("Документы")).toBeInTheDocument();
    expect(within(hse).getByText("Программа курса: математический анализ.pdf")).toBeInTheDocument();
    expect(within(hse).queryByRole("link", { name: /Документы и вложения/ })).toBeNull();
    expect(within(hse).queryByRole("link", { name: /^Источники$/ })).toBeNull();
    expect(screen.queryByText(/Мои материалы/)).toBeNull();

    // У ЦУ свой набор: ошибка источника видна и предлагает починку.
    const cu = await studyPanel(/^Центральный университет/);
    expect(within(cu).getByText("Ошибка")).toBeInTheDocument();
    expect(within(cu).getByRole("link", { name: "Починить" })).toBeInTheDocument();
  });

  it("v4-API недоступен (движок v3): экран откатывается на подключения, без ошибки", async () => {
    current.server.use(http.get("*/api/v1/sources", () => notFoundV4()));
    renderApp("/study");

    const hse = await studyPanel(/^Высшая школа экономики/);
    await waitFor(() => expect(hse).toHaveAttribute("data-source-mode", "connections"));
    const sources = await within(hse).findByRole("list", { name: "Источники: ВШЭ" });
    await waitFor(() => expect(within(sources).getAllByRole("listitem")).toHaveLength(2));
    expect(within(sources).getByText("Расписание (календарь)")).toBeInTheDocument();
    expect(within(sources).getAllByText("Работает").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Не удалось|Ошибка загрузки/)).toBeNull();
  });

  it("карточка источника: что читает, когда обновлялся, «Открыть», «Починить», «Спросить агента»", async () => {
    installSources(connectedSources);
    const user = userEvent.setup();
    renderApp("/study");
    const hse = await studyPanel(/^Высшая школа экономики/);

    await user.click(await within(hse).findByRole("button", { name: /^Личный кабинет: Работает/ }));
    const activeDialog = await screen.findByRole("dialog", { name: "Личный кабинет" });
    expect(within(activeDialog).getByText("Последняя синхронизация")).toBeInTheDocument();
    expect(within(activeDialog).getByText("Настроен: агент знает, как читать источник")).toBeInTheDocument();
    const open = within(activeDialog).getByRole("link", { name: "Открыть сайт: Личный кабинет" });
    expect(open).toHaveAttribute("href", "https://lk.fakeuni.test/");
    expect(open).toHaveAttribute("target", "_blank");
    expect(within(activeDialog).queryByRole("link", { name: /Починить/ })).toBeNull();
    const ask = within(activeDialog).getByRole("link", { name: "Спросить агента: Личный кабинет" });
    expect(hrefOf(ask).pathname).toBe("/chat");
    expect(hrefOf(ask).draft).toBeTruthy();
    await user.keyboard("{Escape}");

    await user.click(await within(hse).findByRole("button", { name: /^Smart LMS: Нужен вход/ }));
    const brokenDialog = await screen.findByRole("dialog", { name: "Smart LMS" });
    expect(within(brokenDialog).getByText("Сессия источника протухла")).toBeInTheDocument();
    const repair = within(brokenDialog).getByRole("link", { name: "Починить: Smart LMS" });
    expect(hrefOf(repair).pathname).toBe("/chat");
    expect(hrefOf(repair).draft).toMatch(/[Вв]ойди снова/);
  });

  it("источник ждёт действия: быстрая кнопка «Войти снова» ведёт в чат из строки", async () => {
    installSources(connectedSources);
    renderApp("/study");
    const hse = await studyPanel(/^Высшая школа экономики/);
    const quick = await within(hse).findByRole("link", { name: "Войти снова: Smart LMS" });
    expect(hrefOf(quick).pathname).toBe("/chat");
    expect(hrefOf(quick).draft).toMatch(/[Вв]ойди снова/);
  });

  it("у вуза нет источников: честное пустое состояние и «Добавить источник»", async () => {
    installSources(() => []);
    renderApp("/study");
    const hse = await studyPanel(/^Высшая школа экономики/);
    expect(await within(hse).findByText(/У вуза пока нет источников/)).toBeInTheDocument();
    expect(within(hse).getByRole("link", { name: "Добавить источник" })).toBeInTheDocument();
    expect(within(hse).getByText("Источников пока нет")).toBeInTheDocument();
  });

  it("без флага остаётся текущий экран: «Документы и вложения» на месте", async () => {
    window.localStorage.setItem(FLAG, "0");
    renderApp("/study");
    const hse = await studyPanel(/^Высшая школа экономики/);
    expect(await within(hse).findByRole("link", { name: /Документы и вложения/ })).toBeInTheDocument();
    expect(document.querySelector("[data-source-mode]")).toBeNull();
  });
});

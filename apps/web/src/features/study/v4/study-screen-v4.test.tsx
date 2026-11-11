import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { Scenario } from "@/mocks/db";
import { hrefOf } from "@/test/data-fixtures";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();

function useScenario(scenario: Scenario = "connected") {
  current.server.close();
  current = createDataServer(scenario);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => useScenario());
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

const hseId = () => (current.db.spaces[0] as { id: string }).id;
const panel = (name: RegExp) => screen.findByRole("region", { name });

const problem = (status: number, code: string, detail: string) =>
  new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );

/** Подменяет ответ нового API источников: так проверяются пустота, отсутствие v4-API и настоящий сбой. */
function overrideSources(handler: (request: Request) => Response | Promise<Response>) {
  current.server.use(http.get("*/api/v1/sources", ({ request }) => handler(request)));
}

describe("«Учёба» по умолчанию: v4-источники без флага", () => {
  it("вузы, их источники из нового API, документы вуза и действия — без лишних разделов", async () => {
    renderApp("/study?view=sources");
    expect(await screen.findByRole("heading", { level: 1, name: "Учёба" })).toBeInTheDocument();

    const hse = await panel(/^Высшая школа экономики/);
    await waitFor(() => expect(hse).toHaveAttribute("data-source-mode", "v4"));
    const list = within(hse).getByRole("list", { name: "Источники: ВШЭ" });
    await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(2));
    expect(within(list).getByText("Личный кабинет")).toBeInTheDocument();
    expect(within(list).getByText("Расписание (календарь)")).toBeInTheDocument();
    // Статус — словами и иконкой, время обновления — подписью; вид данных — словами.
    expect(within(list).getAllByText("Работает").length).toBeGreaterThan(0);
    expect(within(list).getAllByText(/Обновлено/).length).toBeGreaterThan(0);
    expect(within(list).getByText("Расписание")).toBeInTheDocument();

    // Документы — в карточке вуза: это артефакты его источников, отдельного места для них нет.
    expect(within(hse).getByText("Программа курса: математический анализ.pdf")).toBeInTheDocument();
    expect(within(hse).queryByRole("link", { name: /Документы и вложения/ })).toBeNull();
    expect(within(hse).queryByRole("link", { name: /^Источники$/ })).toBeNull();
    expect(screen.queryByText(/Мои материалы/)).toBeNull();

    // «Добавить источник» и «Спросить агента» ведут в чат с готовой просьбой, а не в мастер.
    const add = within(hse).getByRole("link", { name: "Добавить источник" });
    expect(hrefOf(add).pathname).toBe("/chat");
    expect(hrefOf(add).draft).toContain("Высшая школа экономики");
    expect(within(hse).getByRole("link", { name: "Спросить агента" })).toBeInTheDocument();
  });

  it("несколько источников расписания: роль основного и дополнительного подписана", async () => {
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    const list = await within(hse).findByRole("list", { name: "Источники: ВШЭ" });
    await waitFor(() => expect(within(list).getByText("Основное расписание")).toBeInTheDocument());
    expect(within(list).getByText("Дополнительное расписание")).toBeInTheDocument();
  });

  it("старые места «Источники» и «Документы и вложения» ведут на «Учёбу»", async () => {
    for (const retired of ["/study/connections", "/study/documents"]) {
      const { unmount } = renderApp(retired);
      expect(await screen.findByRole("heading", { level: 1, name: "Учёба" })).toBeInTheDocument();
      unmount();
    }
  });

  it("у вуза нет источников: честное пустое состояние и «Добавить источник»", async () => {
    overrideSources(() => HttpResponse.json([]));
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    expect(await within(hse).findByText(/У вуза пока нет источников/)).toBeInTheDocument();
    expect(within(hse).getByText("Источников пока нет")).toBeInTheDocument();
    expect(within(hse).getByRole("link", { name: "Добавить источник" })).toBeInTheDocument();
  });

  it("v4-API выключен (404): бесшовный откат на прежние подключения", async () => {
    current.server.use(http.get("*/api/v1/sources", () => problem(404, "not_found", "v4 выключен")));
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    await waitFor(() => expect(hse).toHaveAttribute("data-source-mode", "connections"));
    const list = await within(hse).findByRole("list", { name: "Источники: ВШЭ" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Расписание (календарь)")).toBeInTheDocument();
    expect(screen.queryByText(/Не удалось загрузить источники/)).toBeNull();
  });

  it("настоящая ошибка 5xx видна, а не прячется за старыми данными", async () => {
    overrideSources(() => problem(500, "internal_error", "Попробуйте позже"));
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    expect(await within(hse).findByText(/Не удалось загрузить источники/)).toBeInTheDocument();
    expect(within(hse).queryByRole("list", { name: "Источники: ВШЭ" })).toBeNull();
  });
});

describe("карточка источника", () => {
  it("что это, что читает, описание чтения, ревизии, прочитанное и история запусков", async () => {
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    await user.click(await within(hse).findByRole("button", { name: /^Личный кабинет:/ }));
    const card = await screen.findByRole("dialog", { name: "Личный кабинет" });

    expect(within(card).getByText("Последняя синхронизация")).toBeInTheDocument();
    // Расписание автообновления — из SourceOut.nextSyncAt, а не выдуманное.
    expect(within(card).getByText("Следующее обновление")).toBeInTheDocument();
    expect(within(card).getByText(/18:25/)).toBeInTheDocument();
    // Playbook: формат, ревизия и проверка; история ревизий.
    expect(within(card).getByText("Декларативное описание")).toBeInTheDocument();
    expect(within(card).getByText("Ревизия 2")).toBeInTheDocument();
    expect(within(card).getByText("Активна")).toBeInTheDocument();
    // Артефакты источника — прочитанные страницы и документы с датой.
    expect(within(card).getByText("Прочитанное")).toBeInTheDocument();
    expect(within(card).getByText("Сроки заданий")).toBeInTheDocument();
    expect(within(card).getByText("Программа курса: математический анализ.pdf")).toBeInTheDocument();
    // История запусков — настоящие прогоны источника со статусами, счётчиками и ссылками.
    const runs = within(card).getByRole("list", { name: "Запуски источника" });
    expect(within(runs).getAllByRole("listitem")).toHaveLength(3);
    expect(within(runs).getByText("Обновление")).toBeInTheDocument();
    expect(within(runs).getByText("Подключение")).toBeInTheDocument();
    expect(within(runs).getByText("Починка")).toBeInTheDocument();
    expect(within(runs).getAllByText("Готово")).toHaveLength(2);
    expect(within(runs).getByText("Не удалось")).toBeInTheDocument();
    expect(within(runs).getByRole("link", { name: "Трасса" })).toHaveAttribute(
      "href",
      "/agent/runs/run-answer",
    );
    // Тред последней задачи источника открывается из карточки (SourceOut.threadId).
    expect(within(card).getByRole("link", { name: "Открыть тред задачи" })).toHaveAttribute(
      "href",
      "/chat?thread=01JMKCDB00000000000000THR",
    );

    expect(within(card).getByRole("link", { name: "Открыть сайт: Личный кабинет" })).toHaveAttribute(
      "href",
      "https://lk.fakeuni.test/",
    );
    expect(hrefOf(within(card).getByRole("link", { name: "Спросить агента: Личный кабинет" })).pathname).toBe(
      "/chat",
    );
    // Пока источник здоров, просьбы «починить» нет.
    expect(within(card).queryByRole("link", { name: /Починить: Личный кабинет/ })).toBeNull();
  });

  it("«Перечитать»: запрос к API и честный итог прогона", async () => {
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    await user.click(await within(hse).findByRole("button", { name: /^Личный кабинет:/ }));
    const card = await screen.findByRole("dialog", { name: "Личный кабинет" });
    const before = current.db.sources.find((source) => source.id === "src-hse-portal")?.lastSyncAt;

    await user.click(within(card).getByRole("button", { name: "Перечитать" }));
    await waitFor(() =>
      expect(current.db.sources.find((source) => source.id === "src-hse-portal")?.lastSyncAt).not.toBe(
        before,
      ),
    );
    expect(await screen.findByText("Источник перечитан")).toBeInTheDocument();
    // После sync сервер назначил следующее обновление, и карточка показывает свежее время.
    await waitFor(() => expect(within(card).getByText(/20:40/)).toBeInTheDocument());
  });

  it("сломанный источник: «Починить» вызывает починку и возвращает статус", async () => {
    const broken = current.db.sources.find((source) => source.id === "src-hse-portal");
    if (!broken) throw new Error("нет источника");
    broken.status = "error";
    broken.lastError = { code: "recipe_drift", detail: "Сайт вуза изменил разметку." };
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    await user.click(await within(hse).findByRole("button", { name: /^Личный кабинет:/ }));
    const card = await screen.findByRole("dialog", { name: "Личный кабинет" });
    expect(within(card).getByText("Сайт вуза изменил разметку.")).toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Починить" }));
    await waitFor(() =>
      expect(current.db.sources.find((source) => source.id === "src-hse-portal")?.status).toBe("active"),
    );
    expect(await screen.findByText("Источник обновлён после починки")).toBeInTheDocument();
    expect(await within(card).findByText("Работает")).toBeInTheDocument();
  });

  it("«Выключить источник» просит подтверждения, «Включить» возвращает его в работу", async () => {
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    await user.click(await within(hse).findByRole("button", { name: /^Личный кабинет:/ }));
    const card = await screen.findByRole("dialog", { name: "Личный кабинет" });

    await user.click(within(card).getByRole("button", { name: "Выключить источник" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Выключить «Личный кабинет»?" });
    expect(within(dialog).getByText(/Ничего не удаляется/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Выключить источник" }));
    await waitFor(() =>
      expect(current.db.sources.find((source) => source.id === "src-hse-portal")?.status).toBe("disabled"),
    );
    expect(await within(card).findByText("Выключен")).toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Включить" }));
    await waitFor(() =>
      expect(current.db.sources.find((source) => source.id === "src-hse-portal")?.status).toBe("active"),
    );
  });

  it("сервер отказал в действии: человеческий тост, а статус не выдаёт успех", async () => {
    current.server.use(
      http.post("*/api/v1/sources/:sourceId/disable", () =>
        problem(500, "internal_error", "Сейчас не получилось — попробуем в фоне."),
      ),
    );
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    await user.click(await within(hse).findByRole("button", { name: /^Личный кабинет:/ }));
    const card = await screen.findByRole("dialog", { name: "Личный кабинет" });
    await user.click(within(card).getByRole("button", { name: "Выключить источник" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Выключить «Личный кабинет»?" });
    await user.click(within(dialog).getByRole("button", { name: "Выключить источник" }));
    expect(await screen.findByText("Не удалось выключить источник")).toBeInTheDocument();
    expect(await screen.findByText("Сейчас не получилось — попробуем в фоне.")).toBeInTheDocument();
    expect(within(card).getByText("Работает")).toBeInTheDocument();
    expect(current.db.sources.find((source) => source.id === "src-hse-portal")?.status).toBe("active");
  });

  it("«Удалить источник»: опасное подтверждение, удаление и обновление списка", async () => {
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    const sources = await within(hse).findByRole("list", { name: "Источники: ВШЭ" });
    expect(within(sources).getAllByRole("listitem")).toHaveLength(2);
    await user.click(within(sources).getByRole("button", { name: /^Личный кабинет:/ }));
    const card = await screen.findByRole("dialog", { name: "Личный кабинет" });

    await user.click(within(card).getByRole("button", { name: "Удалить источник" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Удалить «Личный кабинет»?" });
    expect(within(dialog).getByText(/Данные источника исчезнут/)).toBeInTheDocument();
    expect(within(dialog).getByText(/«Выключить источник» — это обратимо/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Удалить источник" }));

    await waitFor(() =>
      expect(current.db.sources.some((source) => source.id === "src-hse-portal")).toBe(false),
    );
    expect(await screen.findByText("Источник удалён")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Личный кабинет" })).toBeNull());
    await waitFor(() => expect(within(sources).getAllByRole("listitem")).toHaveLength(1));
  });

  it("удаление при незавершённой задаче: 409 человеческим текстом, источник остаётся", async () => {
    current.server.use(
      http.delete("*/api/v1/sources/:sourceId", () =>
        problem(
          409,
          "conflict",
          "У источника идёт синхронизация. Дождитесь её окончания и попробуйте снова.",
        ),
      ),
    );
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    await user.click(await within(hse).findByRole("button", { name: /^Личный кабинет:/ }));
    const card = await screen.findByRole("dialog", { name: "Личный кабинет" });
    await user.click(within(card).getByRole("button", { name: "Удалить источник" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Удалить «Личный кабинет»?" });
    await user.click(within(dialog).getByRole("button", { name: "Удалить источник" }));
    expect(await screen.findByText("Источник сейчас обновляется")).toBeInTheDocument();
    expect(await screen.findByText(/У источника идёт синхронизация/)).toBeInTheDocument();
    expect(current.db.sources.some((source) => source.id === "src-hse-portal")).toBe(true);
    expect(screen.getByRole("dialog", { name: "Личный кабинет" })).toBeInTheDocument();
  });

  it("«Сделать основным для расписания» меняет порядок источников вида", async () => {
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const hse = await panel(/^Высшая школа экономики/);
    await user.click(await within(hse).findByRole("button", { name: /^Расписание \(календарь\):/ }));
    const card = await screen.findByRole("dialog", { name: "Расписание (календарь)" });
    await user.click(within(card).getByRole("button", { name: /Сделать основным для расписания/ }));
    await waitFor(() =>
      expect(current.db.sourcePriorities[`${hseId()}:schedule`]).toEqual([
        "src-hse-calendar",
        "src-hse-portal",
      ]),
    );
    expect(await screen.findByText(/Основное расписание — «Расписание \(календарь\)»/)).toBeInTheDocument();
  });

  it("источник в прежней модели: карточка честно говорит, что это fallback", async () => {
    current.server.use(http.get("*/api/v1/sources", () => problem(404, "not_found", "v4 выключен")));
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const cu = await panel(/^Центральный университет/);
    await user.click(await within(cu).findByRole("button", { name: /^LMS:/ }));
    const card = await screen.findByRole("dialog", { name: "LMS" });
    expect(within(card).getByText(/прежняя модель подключений/)).toBeInTheDocument();
    expect(within(card).getByText(/истории не отдаёт/)).toBeInTheDocument();
    expect(within(card).queryByRole("list", { name: "Запуски источника" })).toBeNull();
  });

  it("источник ждёт входа: карточка ведёт в чат, а не в отдельную форму", async () => {
    const lms = current.db.sources.find((source) => source.id === "src-cu-lms");
    if (!lms) throw new Error("нет источника");
    lms.status = "needs_reauth";
    lms.lastError = { code: "source_auth_required", detail: "Нужно снова войти." };
    const user = userEvent.setup();
    renderApp("/study?view=sources");
    const cu = await panel(/^Центральный университет/);
    await user.click(await within(cu).findByRole("button", { name: /^LMS:/ }));
    const card = await screen.findByRole("dialog", { name: "LMS" });
    expect(within(card).getByText("Нужен вход")).toBeInTheDocument();
    const login = within(card).getByRole("link", { name: "Войти снова: LMS" });
    expect(hrefOf(login).pathname).toBe("/chat");
    expect(hrefOf(login).draft).toContain("Войди снова");
  });
});

describe("«Мои материалы»", () => {
  it("фильтруются из директории вузов", () => {
    renderApp("/study?view=sources");
    // Вымышленное пространство без вуза могло бы прийти от сервера до миграции: интерфейс его не рисует.
    expect(screen.queryByText("Мои материалы")).toBeNull();
  });
});

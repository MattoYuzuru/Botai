import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { DataVariant } from "@/mocks/data-handlers";
import type { Scenario } from "@/mocks/db";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();

function useScenario(scenario: Scenario, variant: DataVariant | null = null) {
  current.server.close();
  current = createDataServer(scenario, variant);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => useScenario("connected"));
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
  useActiveSpaceStore.setState({ id: "all" });
  vi.restoreAllMocks();
  window.dispatchEvent(new Event("online"));
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const hseId = () => (current.db.spaces[0] as { id: string }).id;
const cuId = () => (current.db.spaces[1] as { id: string }).id;
const screenState = (kind: string) =>
  document.querySelector(`[data-screen=${kind}]`)?.getAttribute("data-state");
const panel = (name: RegExp) => screen.findByRole("region", { name });
const titles = (list: HTMLElement) =>
  within(list)
    .getAllByRole("link")
    .map((link) => link.querySelector("span.type-subheading")?.textContent);

describe("«Учёба»: пространства и динамические разделы", () => {
  it("у каждого вуза свои разделы в фиксированном порядке, только с данными; «Подключения» — всегда", async () => {
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    await waitFor(() => expect(within(hse).getAllByRole("link").length).toBeGreaterThan(1));
    expect(titles(within(hse).getByRole("list", { name: /Разделы/ }))).toEqual([
      "Предметы",
      "Оценки",
      "Преподаватели",
      "Аудитории",
      "Новости",
      "Документы",
      "Подключения",
    ]);
    const cu = await panel(/^Центральный университет/);
    await waitFor(() => expect(within(cu).getAllByRole("link").length).toBeGreaterThan(1));
    // У ЦУ нет аудиторий (онлайн) и документов: этих разделов нет, а не «пусто».
    expect(titles(within(cu).getByRole("list", { name: /Разделы/ }))).toEqual([
      "Предметы",
      "Оценки",
      "Преподаватели",
      "Новости",
      "Подключения",
    ]);
  });

  it("счётчики берутся из ответа сервера, у «Подключений» — число подключений вуза", async () => {
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    const courses = await within(hse).findByRole("link", { name: /Предметы/ });
    expect(courses).toHaveTextContent("4");
    expect(within(hse).getByRole("link", { name: /Подключения/ })).toHaveTextContent("2");
  });

  it("выбранный в переключателе вуз оставляет на экране только его панель", async () => {
    useActiveSpaceStore.setState({ id: cuId() });
    renderApp("/study");
    await panel(/^Центральный университет/);
    expect(screen.queryByRole("region", { name: /^Высшая школа экономики/ })).toBeNull();
  });

  it("профиль и настройки остаются внизу экрана: тема, спокойное движение, выход", async () => {
    renderApp("/study");
    expect(await screen.findByRole("radio", { name: "Тёмная" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Спокойное движение/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Выйти из BotAI" })).toBeInTheDocument();
  });

  it("нет вузов — приглашение подключить; настройки по-прежнему доступны", async () => {
    useScenario("empty");
    renderApp("/study");
    expect(await screen.findByText("Вузов пока нет")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Выйти из BotAI" })).toBeInTheDocument();
  });

  it("вуз подключён, данных нет: остаётся «Подключения», остальные разделы не рисуются пустыми", async () => {
    useScenario("connected", "quiet");
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    await waitFor(() => expect(within(hse).getAllByRole("link")).toHaveLength(1));
    expect(within(hse).getByRole("link", { name: /Подключения/ })).toBeInTheDocument();
  });

  it("нужен повторный вход: полоса «Войти снова» и пометка у «Подключений»", async () => {
    useScenario("reauth");
    renderApp("/study");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(within(notices).getByRole("button", { name: "Войти снова" })).toBeInTheDocument();
    await waitFor(() => expect(screenState("study")).toBe("reauth"));
    expect(await screen.findByText(/Нужно внимание: 1/)).toBeInTheDocument();
  });

  it("раздел открывается по ссылке из панели: адрес несёт вид и вуз", async () => {
    const { router } = renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    await userEvent.click(await within(hse).findByRole("link", { name: /Оценки/ }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/study/grades"));
    expect(router.state.location.search).toEqual({ space: hseId() });
  });
});

describe("«Учёба»: экраны разделов", () => {
  it("«Предметы»: предметы одного вуза с кодом и периодом, карточка с источником и ссылкой", async () => {
    renderApp(`/study/courses?space=${hseId()}`);
    expect(await screen.findByRole("heading", { level: 1, name: "Предметы" })).toBeInTheDocument();
    const row = await screen.findByRole("button", { name: /Дискретная математика/ });
    expect(row).toHaveTextContent("ДМ-102 · Осень 2026");
    expect(screen.queryByText("Data Science")).toBeNull();
    await userEvent.click(row);
    const sheet = await screen.findByRole("dialog", { name: "Дискретная математика" });
    expect(within(sheet).getByText(/Получено/)).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: /Открыть предмет/ })).toHaveAttribute(
      "href",
      expect.stringMatching(/^https:\/\//),
    );
  });

  it("на сводном экране у каждой записи метка вуза", async () => {
    renderApp("/study/courses");
    await screen.findByRole("button", { name: /Data Science/ });
    const row = screen.getByRole("button", { name: /Data Science/ });
    expect(within(row).getByText("ЦУ")).toBeInTheDocument();
    expect(
      within(screen.getByRole("button", { name: /Математический анализ/ })).getByText("ВШЭ"),
    ).toBeInTheDocument();
  });

  it("«Оценки»: плитки по предметам, значения как у источника, без пересчёта и процентов", async () => {
    renderApp(`/study/grades?space=${hseId()}`);
    const group = await screen.findByRole("region", { name: "Математический анализ" });
    expect(within(group).getByText("8")).toBeInTheDocument();
    expect(within(group).getByText("10 баллов")).toBeInTheDocument();
    expect(within(group).getByText("вес 0,2")).toBeInTheDocument();
    const credit = await screen.findByRole("button", { name: /зачтено/ });
    expect(credit).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\d\s*%/);
    await userEvent.click(screen.getByRole("button", { name: /Контрольная №2/ }));
    const sheet = await screen.findByRole("dialog", { name: "Контрольная №2" });
    expect(within(sheet).getByText("Вес")).toBeInTheDocument();
  });

  it("«Преподаватели»: роль и почта; «Написать» — только для настоящего адреса", async () => {
    renderApp(`/study/people?space=${hseId()}`);
    await userEvent.click(await screen.findByRole("button", { name: /Петров Александр Владимирович/ }));
    const sheet = await screen.findByRole("dialog", { name: "Петров Александр Владимирович" });
    expect(within(sheet).getByText("petrov@fakeuni.test")).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Написать" })).toHaveAttribute(
      "href",
      "mailto:petrov@fakeuni.test",
    );
  });

  it("«Новости»: свежие первыми, текст новости — обычный текст, ссылка на оригинал отдельно", async () => {
    renderApp(`/study/news?space=${hseId()}`);
    const rows = await screen.findAllByRole("button", { name: /Изменения в расписании|Открыта запись/ });
    expect(rows[0]).toHaveTextContent("Изменения в расписании на следующей неделе");
    await userEvent.click(rows[0] as HTMLElement);
    const sheet = await screen.findByRole("dialog", { name: "Изменения в расписании на следующей неделе" });
    expect(within(sheet).getByText(/аудиторию 309/)).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: /Открыть новость/ })).toBeInTheDocument();
  });

  it("«Документы»: вид, время получения, карточка со ссылкой", async () => {
    renderApp(`/study/documents?space=${hseId()}`);
    const row = await screen.findByRole("button", { name: /Рейтинг группы/ });
    expect(row).toHaveTextContent("Таблица");
    expect(row).toHaveTextContent(/Получен/);
    await userEvent.click(row);
    expect(await screen.findByRole("link", { name: /Открыть документ/ })).toBeInTheDocument();
  });

  it("«Аудитории»: собраны из расписания, у каждой ближайшая пара и число пар впереди", async () => {
    renderApp(`/study/locations?space=${hseId()}`);
    expect(await screen.findByText(/Собрано из расписания на ближайшие четыре недели/)).toBeInTheDocument();
    const row = await screen.findByRole("button", { name: /ауд\. 305/ });
    // Утренняя пара уже закончилась (17:40): ближайшая в этой аудитории — завтрашняя.
    expect(row).toHaveTextContent(/Ближайшая: «Теория вероятностей», завтра в 13:00/);
    expect(row).toHaveTextContent(/7\sпар/);
    await userEvent.click(row);
    const sheet = await screen.findByRole("dialog", { name: "ауд. 305" });
    expect(within(sheet).getByText(/Впереди/)).toBeInTheDocument();
  });

  it("«Подключения»: источник, адрес без схемы и статус человеческим языком", async () => {
    useScenario("reauth");
    renderApp(`/study/connections?space=${cuId()}`);
    await screen.findByRole("heading", { level: 1, name: "Подключения" });
    await waitFor(() =>
      expect(document.querySelector("li[data-connection-status=needs_reauth]")).not.toBeNull(),
    );
    const item = document.querySelector("li[data-connection-status=needs_reauth]") as HTMLElement;
    expect(item).toHaveTextContent("LMS");
    expect(item).toHaveTextContent("my.fakeuni.test");
    expect(within(item).getByText("Нужно снова войти")).toBeInTheDocument();
    expect(item).not.toHaveTextContent(/needs_reauth|source_auth_required/);
  });

  it("раздел без данных: «Предметов пока нет» без «всё сдано»", async () => {
    useScenario("connected", "quiet");
    renderApp(`/study/courses?space=${hseId()}`);
    expect(await screen.findByText("Предметов пока нет")).toBeInTheDocument();
    await waitFor(() => expect(screenState("study-courses")).toBe("empty"));
    expect(screen.queryByText(/всё сдано/i)).toBeNull();
  });

  it("устаревшие данные не называют пустоту правдой: «Аудиторий» нет в снимке — «может быть неполным»", async () => {
    useScenario("stale", "quiet");
    renderApp(`/study/courses?space=${cuId()}`);
    expect(await screen.findByText(/Они могут быть неполными/)).toBeInTheDocument();
  });

  it("неизвестный вид в адресе — «Такого раздела нет» и путь назад", async () => {
    renderApp("/study/unknown");
    expect(await screen.findByText("Такого раздела нет")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "К разделам" })).toHaveAttribute("href", "/study");
  });

  it("loading: скелетон, пока данных нет, и никаких кнопок повтора", async () => {
    current.server.use(http.get("*/api/v1/courses", () => new Promise(() => {})));
    renderApp(`/study/courses?space=${hseId()}`);
    await waitFor(() => expect(screenState("study-courses")).toBe("loading"));
    expect(document.querySelector("[aria-busy=true]")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
  });

  it("offline: последний снимок и подпись «Нет сети. Показан снимок от …»", async () => {
    renderApp(`/study/news?space=${hseId()}`);
    await screen.findByRole("button", { name: /Изменения в расписании/ });
    vi.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    window.dispatchEvent(new Event("offline"));
    expect((await screen.findAllByText(/Нет сети\. Показан снимок от/)).length).toBeGreaterThan(0);
    await waitFor(() => expect(screenState("study-news")).toBe("offline"));
  });
});

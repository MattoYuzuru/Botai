import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { DataVariant } from "@/mocks/data-handlers";
import type { Scenario } from "@/mocks/db";
import { addConnection, hrefOf } from "@/test/data-fixtures";
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
  it("у каждого вуза свои разделы в фиксированном порядке, только с данными", async () => {
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    await waitFor(() => expect(within(hse).getAllByRole("link").length).toBeGreaterThan(1));
    expect(titles(within(hse).getByRole("list", { name: /Разделы/ }))).toEqual([
      "Предметы",
      "Оценки",
      "Преподаватели",
      "Аудитории",
      "Новости",
      "Документы и вложения",
    ]);
    const cu = await panel(/^Центральный университет/);
    await waitFor(() => expect(within(cu).getAllByRole("link").length).toBeGreaterThan(1));
    // У ЦУ нет аудиторий (онлайн) и документов: этих разделов нет, а не «пусто». Источники — не раздел, а список выше.
    expect(titles(within(cu).getByRole("list", { name: /Разделы/ }))).toEqual([
      "Предметы",
      "Оценки",
      "Преподаватели",
      "Новости",
    ]);
  });

  it("счётчики берутся из ответа сервера", async () => {
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    const courses = await within(hse).findByRole("link", { name: /Предметы/ });
    expect(courses).toHaveTextContent("4");
    expect(within(hse).getByRole("link", { name: /Документы и вложения/ })).toHaveTextContent("3");
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

  it("вуз подключён, данных нет: источники на месте, разделы не рисуются пустыми", async () => {
    useScenario("connected", "quiet");
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    expect(await within(hse).findByText(/Данных пока нет/)).toBeInTheDocument();
    expect(within(hse).queryByRole("list", { name: /Разделы/ })).toBeNull();
    expect(within(hse).getByRole("list", { name: /Источники/ })).toBeInTheDocument();
  });

  it("нужен повторный вход: полоса «Войти снова» ведёт в чат, у источника статус словами", async () => {
    useScenario("reauth");
    renderApp("/study");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(within(notices).getByRole("link", { name: "Войти снова" })).toBeInTheDocument();
    await waitFor(() => expect(screenState("study")).toBe("reauth"));
    const cu = await panel(/^Центральный университет/);
    const source = within(cu).getByRole("list", { name: /Источники/ });
    expect(within(source).getByText("Нужно снова войти")).toBeInTheDocument();
  });

  it("раздел открывается по ссылке из панели: адрес несёт вид и вуз", async () => {
    const { router } = renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    await userEvent.click(await within(hse).findByRole("link", { name: /Оценки/ }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/study/grades"));
    expect(router.state.location.search).toEqual({ space: hseId() });
  });
});

describe("«Учёба»: вуз и его источники", () => {
  /** Вуз с несколькими источниками разных видов: ЛК, Smart LMS, документация, таблица. */
  function connectSeveralSources() {
    addConnection(current.db, hseId(), {
      title: "Smart LMS",
      kind: "lms",
      origin: "https://smart.fakeuni.test",
      entryUrl: "https://smart.fakeuni.test/courses",
      lastSyncAt: new Date(NOW - 20 * 60_000).toISOString(),
    });
    addConnection(current.db, hseId(), {
      title: "Документация для студентов",
      kind: "doc",
      origin: "https://docs.fakeuni.test",
      entryUrl: "https://docs.fakeuni.test/students",
    });
    addConnection(current.db, hseId(), {
      title: "Рейтинг группы",
      kind: "sheet",
      origin: "https://sheets.fakeuni.test",
      entryUrl: null,
      status: "stale",
    });
  }

  it("вуз показан названием и сокращением, источники — человеческими названиями с видом, статусом и обновлением", async () => {
    connectSeveralSources();
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    expect(within(hse).getByText("ВШЭ", { selector: "span" })).toBeInTheDocument();
    const list = await within(hse).findByRole("list", { name: "Источники: ВШЭ" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((row) => row.getAttribute("data-connection-kind"))).toEqual([
      "portal",
      "schedule",
      "lms",
      "doc",
      "sheet",
    ]);
    const lms = rows[2] as HTMLElement;
    expect(lms).toHaveTextContent("Smart LMS");
    expect(lms).toHaveTextContent("Учебная платформа");
    expect(lms).toHaveTextContent("Работает");
    expect(lms).toHaveTextContent(/Обновлено 20\sминут назад/);
    expect(rows[3]).toHaveTextContent("Документация");
    expect(rows[4]).toHaveTextContent("Данные устарели");
    // Адрес сайта не заменяет название ни в одной строке.
    expect(list).not.toHaveTextContent(/fakeuni\.test/);
  });

  it("«Открыть сайт» ведёт на страницу входа в данные источника; у источника без сайта кнопки нет", async () => {
    connectSeveralSources();
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    const list = await within(hse).findByRole("list", { name: "Источники: ВШЭ" });
    const smart = within(list).getByRole("link", { name: "Открыть сайт: Smart LMS" });
    expect(smart).toHaveAttribute("href", "https://smart.fakeuni.test/courses");
    expect(smart).toHaveAttribute("target", "_blank");
    expect(smart).toHaveAttribute("rel", expect.stringContaining("noopener"));
    // Ссылка на источник без entryUrl откатывается к origin, но никогда не к адресу API.
    expect(within(list).getByRole("link", { name: "Открыть сайт: Рейтинг группы" })).toHaveAttribute(
      "href",
      "https://sheets.fakeuni.test/",
    );
    for (const link of within(list).getAllByRole("link"))
      expect(link.getAttribute("href")).not.toMatch(/\/api\//);
  });

  it("«Добавить источник» открывает новый чат с готовой просьбой и названием вуза, а не отдельный мастер", async () => {
    connectSeveralSources();
    const { router } = renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    const add = await within(hse).findByRole("link", { name: "Добавить источник" });
    expect(hrefOf(add).pathname).toBe("/chat");
    expect(hrefOf(add).draft).toContain("Высшая школа экономики");
    await userEvent.click(add);
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(router.state.location.search.draft).toContain("Высшая школа экономики");
    expect(router.state.location.search.thread).toBeUndefined();
  });

  it("«Подключить ещё вуз» — чат с открытой фразой; ссылок на /connect на экране нет", async () => {
    renderApp("/study");
    const more = await screen.findByRole("link", { name: "Подключить ещё вуз" });
    expect(hrefOf(more).pathname).toBe("/chat");
    expect(hrefOf(more).draft).toBeTruthy();
    expect(hrefOf(more).thread).toBeUndefined();
    await panel(/^Высшая школа экономики/);
    expect(document.querySelector('a[href^="/connect"]')).toBeNull();
  });

  it("плашка источника, который ждёт ответа, без треда задачи ведёт в чат с готовой просьбой", async () => {
    useScenario("connected", "waiting");
    renderApp("/study");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    const link = hrefOf(within(notices).getByRole("link", { name: "Продолжить" }));
    expect(link.pathname).toBe("/chat");
    expect(link.draft).toContain("Личный кабинет");
    expect(link.draft).toContain("Высшая школа экономики");
    expect(link.thread).toBeUndefined();
  });

  it("плашка источника с тредом задачи ведёт прямо в него", async () => {
    useScenario("connected", "waiting");
    const waiting = current.db.connections.find((row) => row.status === "waiting_user");
    if (waiting) waiting.threadId = "01JTHREADWAIT0000000000AA";
    renderApp("/study");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(hrefOf(within(notices).getByRole("link", { name: "Продолжить" }))).toEqual({
      pathname: "/chat",
      thread: "01JTHREADWAIT0000000000AA",
    });
  });

  it("«Добавить документ» есть в панели вуза, даже когда документов ещё нет", async () => {
    renderApp("/study");
    // У ЦУ нет документов: строки «Документы и вложения» в списке разделов нет, а добавить документ можно.
    const cu = await panel(/^Центральный университет/);
    await within(cu).findByRole("list", { name: "Источники: ЦУ" });
    expect(within(cu).queryByRole("link", { name: /Документы и вложения/ })).toBeNull();
    await userEvent.click(within(cu).getByRole("button", { name: "Добавить документ" }));
    const sheet = await screen.findByRole("dialog", { name: "Добавить документ" });
    expect(within(sheet).getByLabelText("Ссылка")).toBeInTheDocument();
  });

  it("строка источника открывает карточку: сайт, чат источника и что агент делает сейчас", async () => {
    const task = { jobId: "01JJOB000000000000000000AA", kind: "sync" as const, state: "running" as const };
    const portal = current.db.connections.find((row) => row.title === "Личный кабинет");
    if (portal) Object.assign(portal, { threadId: "01JTHREADPORTAL00000000AA", activeTask: task });
    renderApp("/study");
    const hse = await panel(/^Высшая школа экономики/);
    await userEvent.click(await within(hse).findByRole("button", { name: /Личный кабинет/ }));
    const sheet = await screen.findByRole("dialog", { name: "Личный кабинет" });
    expect(within(sheet).getByText("Обновление данных: идёт")).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: /Открыть сайт/ })).toHaveAttribute(
      "href",
      "https://lk.fakeuni.test/",
    );
    expect(hrefOf(within(sheet).getByRole("link", { name: /Открыть чат источника/ }))).toEqual({
      pathname: "/chat",
      thread: "01JTHREADPORTAL00000000AA",
    });
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

  it("«Источники»: вид, статус человеческим языком и сайт источника", async () => {
    useScenario("reauth");
    renderApp(`/study/connections?space=${cuId()}`);
    await screen.findByRole("heading", { level: 1, name: "Источники" });
    await waitFor(() =>
      expect(document.querySelector("li[data-connection-status=needs_reauth]")).not.toBeNull(),
    );
    const item = document.querySelector("li[data-connection-status=needs_reauth]") as HTMLElement;
    expect(item).toHaveTextContent("LMS");
    expect(item).toHaveTextContent("Учебная платформа");
    expect(within(item).getByText("Нужно снова войти")).toBeInTheDocument();
    expect(within(item).getByRole("link", { name: "Открыть сайт: LMS" })).toHaveAttribute(
      "href",
      "https://my.fakeuni.test/learn",
    );
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

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
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

const trace = (runId: string) => renderApp(`/agent/runs/${runId}`);
const heading = () => screen.findByRole("heading", { level: 1, name: "Трасса агента" });

describe("трасса хода агента", () => {
  it("завершённый ход: состояние, задача, счётчики, шаги и артефакты", async () => {
    trace("run-answer");
    expect(await heading()).toBeInTheDocument();
    expect(await screen.findByText("Завершено")).toBeInTheDocument();
    expect(screen.getByText(/Разбери расписание на неделю/)).toBeInTheDocument();
    expect(screen.getByText("4 322 токена")).toBeInTheDocument();
    expect(screen.getAllByText(/deepseek-chat/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\d{2}:\d{2}/).length).toBeGreaterThan(0);

    const steps = await screen.findByRole("list", { name: "Шаги трассы" });
    await waitFor(() => expect(within(steps).getAllByRole("listitem")).toHaveLength(6));
    expect(within(steps).getByText("Запуск")).toBeInTheDocument();
    expect(within(steps).getByText("Вызов инструмента: sources_sync")).toBeInTheDocument();
    expect(within(steps).getByText("Результат инструмента: sources_sync")).toBeInTheDocument();
    expect(within(steps).getAllByText("Ответ модели: deepseek-chat")).toHaveLength(2);
    expect(within(steps).getByText("Выполнено")).toBeInTheDocument();
    expect(within(steps).getByText("Итог")).toBeInTheDocument();

    expect(screen.getByText("Артефакты")).toBeInTheDocument();
    expect(screen.getByText("artifact-schedule-1")).toBeInTheDocument();
  });

  it("подробности шага раскрываются и скрываются, текст модели — обычный текст", async () => {
    const user = userEvent.setup();
    trace("run-answer");
    await screen.findByText("Завершено");
    const modelTitles = await screen.findAllByText("Ответ модели: deepseek-chat");
    const card = modelTitles[0]?.closest("li");
    if (!card) throw new Error("нет карточки шага");
    const toggle = within(card).getByRole("button", { name: "Показать подробности" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(within(card).getByText("Текст модели")).toBeInTheDocument();
    expect(within(card).getByText(/Посмотрю расписание/)).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Свернуть подробности" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("идущий ход: честный рельс без процентов и подпись, что шаги придут сами", async () => {
    trace("run-running");
    expect(await screen.findByText("Выполняется")).toBeInTheDocument();
    expect(document.querySelector('[data-part="run-live"]')).not.toBeNull();
    expect(await screen.findByText("Новые шаги появятся здесь сами.")).toBeInTheDocument();
    // Процентов и ETA нет: рельс не обещает конца.
    expect(document.body.textContent).not.toMatch(/\d+\s*%/);
  });

  it("лимит шагов: остановка словами и числа из трассы", async () => {
    trace("run-limit");
    expect(await screen.findByText("Остановлено лимитом шагов")).toBeInTheDocument();
    const note = document.querySelector('[data-part="run-limit"]');
    expect(note?.textContent).toContain("Ход остановлен: шаги");
    expect(note?.textContent).toContain("шагов: 40");
  });

  it("пауза: ход остановлен, об этом сказано словами", async () => {
    trace("run-paused");
    expect(await screen.findByText("Пауза")).toBeInTheDocument();
    expect(document.querySelector('[data-part="run-paused"]')?.textContent).toContain("Ход на паузе");
  });

  it("сбой: человеческий текст ошибки и код отдельно", async () => {
    trace("run-error");
    expect(await screen.findByText("Сбой")).toBeInTheDocument();
    const note = document.querySelector('[data-part="run-error"]');
    expect(note?.textContent).toContain("Провайдер модели завершился ошибкой.");
    expect(note?.textContent).toContain("llm_error");
  });

  it("трассы нет на сервере (движок v3): честное пустое состояние, не бесконечный скелетон", async () => {
    current.server.use(
      http.get("*/api/v1/agent/runs/:runId", () =>
        problem(404, "not_found", "Агент v4 выключен: включите BOTAI_AGENT_ENGINE=v4"),
      ),
    );
    trace("run-answer");
    expect(await screen.findByText("Трассы на этом сервере нет")).toBeInTheDocument();
    expect(screen.queryByText("Загрузка трассы")).toBeNull();
    expect(document.querySelector('[data-screen="agent-run"]')).toHaveAttribute("data-state", "empty");
  });

  it("настоящая ошибка сервера видна, а не выдаётся за пустоту", async () => {
    current.server.use(
      http.get("*/api/v1/agent/runs/:runId", () =>
        problem(500, "internal_error", "Трасса временно недоступна"),
      ),
    );
    trace("run-answer");
    expect(await screen.findByText(/Трасса не загрузилась/)).toBeInTheDocument();
    expect(screen.queryByText("Трассы на этом сервере нет")).toBeNull();
  });

  it("шаги страницами: «Показать ещё» догружает остаток трассы", async () => {
    const user = userEvent.setup();
    trace("run-long");
    const steps = await screen.findByRole("list", { name: "Шаги трассы" });
    await waitFor(() => expect(within(steps).getAllByRole("listitem")).toHaveLength(100));
    const more = screen.getByRole("button", { name: "Показать ещё" });
    await user.click(more);
    await waitFor(() => expect(within(steps).getAllByRole("listitem")).toHaveLength(107));
    expect(screen.queryByRole("button", { name: "Показать ещё" })).toBeNull();
  });

  it("пустая трасса: честный текст вместо пустого списка", async () => {
    trace("run-empty");
    expect(await screen.findByText("Событий в трассе нет.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Шаги трассы" })).toBeNull();
  });

  it("a11y: один h1, список шагов с именем, у шага есть доступные подробности", async () => {
    trace("run-answer");
    await heading();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const steps = await screen.findByRole("list", { name: "Шаги трассы" });
    expect(steps.tagName).toBe("OL");
    expect(within(steps).getAllByRole("listitem")).toHaveLength(6);
    expect((await screen.findAllByRole("button", { name: "Показать подробности" }))[0]).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});

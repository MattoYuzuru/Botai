import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => {
  current.server.close();
  current = createDataServer("connected");
  current.server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const memory = () => screen.findByRole("region", { name: "Что помнит агент" });

describe("Настройки → «Что помнит агент» (D-045)", () => {
  it("правила для агента отдельно от заметок; правила статусов; как их создать — словами", async () => {
    renderApp("/study?view=sources");
    const section = await memory();
    const rules = await within(section).findByRole("region", { name: "Правила для агента" });
    expect(within(rules).getAllByRole("listitem")).toHaveLength(2);
    expect(rules).toHaveTextContent("Про оценки сначала смотри LMS ЦУ");
    const notes = within(section).getByRole("region", { name: "Заметки" });
    expect(notes).toHaveTextContent("Учится на 3 курсе");
    expect(notes).not.toHaveTextContent("Про оценки сначала");
    const statuses = within(section).getByRole("region", { name: "Правила статусов" });
    expect(statuses).toHaveTextContent("«Решение прикреплено» значит «Сдано»");
    expect(statuses).toHaveTextContent("Личный кабинет");
    expect(section).toHaveTextContent(/просите агента в чате/);
  });

  it("удаление правила — через подтверждение; запись уходит с сервера и из списка", async () => {
    renderApp("/study?view=sources");
    const section = await memory();
    await userEvent.click(
      await within(section).findByRole("button", { name: /Удалить правило «Сроки заданий называй/ }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "Удалить правило?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByText("Правило удалено")).toBeInTheDocument();
    await waitFor(() => expect(current.db.memory.map((item) => item.id)).not.toContain("mem-rule-2"));
    await waitFor(() => expect(section).not.toHaveTextContent("Сроки заданий называй"));
  });

  it("удаление правила статуса возвращает заданию слова источника", async () => {
    renderApp("/study?view=sources");
    const section = await memory();
    await userEvent.click(
      await within(section).findByRole("button", { name: "Удалить правило статуса «Решение прикреплено»" }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "Удалить правило статуса?" });
    expect(dialog).toHaveTextContent("по словам источника");
    await userEvent.click(within(dialog).getByRole("button", { name: "Удалить" }));
    await waitFor(() => expect(current.db.statusRules).toHaveLength(0));
    await waitFor(() =>
      expect(within(section).queryByRole("region", { name: "Правила статусов" })).toBeNull(),
    );
  });

  it("сервер без правил статусов (404) — остальная память на месте", async () => {
    current.server.use(
      http.get("*/api/v1/status-rules", () =>
        HttpResponse.json(
          { type: "about:blank", title: "not_found", status: 404, code: "not_found", detail: "Нет" },
          { status: 404, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );
    renderApp("/study?view=sources");
    const section = await memory();
    expect(await within(section).findByRole("region", { name: "Заметки" })).toBeInTheDocument();
    expect(within(section).queryByRole("region", { name: "Правила статусов" })).toBeNull();
  });
});

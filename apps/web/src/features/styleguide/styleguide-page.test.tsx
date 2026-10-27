import { motionPreference, STORAGE_KEYS, themePreference } from "@botai/design";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { StyleguidePage } from "./styleguide-page";

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-motion");
  document.documentElement.style.fontSize = "";
});

describe("/styleguide", () => {
  it("показывает все разделы витрины", () => {
    render(<StyleguidePage />);
    expect(screen.getByRole("heading", { level: 1, name: "Витрина компонентов" })).toBeInTheDocument();
    for (const title of [
      "Палитра",
      "Типографика",
      "Действия и ввод",
      "Состояния и статус",
      "Учебные данные",
      "Чат и агент",
      "Блоки агента",
      "ProgressWall",
      "Навигация и слои",
      "Варя",
    ]) {
      expect(screen.getByRole("heading", { level: 2, name: title })).toBeInTheDocument();
    }
  });

  it("витрина Вари: фикстуры §8 на месте и дают ожидаемый результат", () => {
    const { container } = render(<StyleguidePage />);
    for (const id of [
      "urgent",
      "urgent-reduced",
      "urgent-reauth",
      "overdue",
      "due-unknown",
      "done-once",
      "no-data",
      "stale-after-failed-sync",
      "space-switch",
      "thinking",
      "new-year-urgent",
      "seasonal-off",
      "calm",
      "lite",
      "hidden",
    ]) {
      expect(container.querySelector(`[data-fixture="${id}"]`), id).not.toBeNull();
    }
    const resolved = (id: string) =>
      container.querySelector(`[data-fixture="${id}"] [data-testid="resolved"]`)?.textContent ?? "";
    expect(resolved("urgent")).toContain("deadline · animated");
    expect(resolved("urgent-reduced")).toContain("deadline · static");
    expect(resolved("urgent-reauth")).toContain("reauth");
    expect(resolved("urgent-reauth")).not.toContain("new-year");
    expect(resolved("new-year-urgent")).toContain("new-year");
    expect(resolved("seasonal-off")).not.toContain("new-year");
    expect(resolved("no-data")).toContain("connect · static");
    expect(resolved("stale-after-failed-sync")).toContain("idle");
    expect(resolved("hidden")).toContain("без картинки");
    // Бейдж срочной работы и действие — текстом рядом с картинкой.
    const urgent = container.querySelector('[data-fixture="urgent"]');
    expect(urgent).toHaveTextContent(/Осталось\s5\sч/);
    expect(urgent).toHaveTextContent("Открыть срочную");
  });

  it("на витрине нет ни одной кнопки «Повторить», «Обновить» или «Загрузить»", () => {
    render(<StyleguidePage />);
    for (const button of screen.getAllByRole("button")) {
      expect(button.textContent?.trim()).not.toMatch(/^(Повторить|Обновить|Загрузить|Попробовать снова)$/);
    }
  });

  it("обе темы рядом: две панели с принудительной темой", async () => {
    render(<StyleguidePage />);
    await userEvent.click(screen.getByRole("switch", { name: /Светлая и тёмная рядом/ }));
    expect(screen.getByTestId("panel-light")).toHaveAttribute("data-theme", "light");
    expect(screen.getByTestId("panel-dark")).toHaveAttribute("data-theme", "dark");
    // Каждая панель содержит полную галерею.
    for (const panel of ["panel-light", "panel-dark"]) {
      expect(
        within(screen.getByTestId(panel)).getByRole("heading", { level: 2, name: "Чат и агент" }),
      ).toBeInTheDocument();
    }
  });

  it("выбор темы, акцента, зума и спокойного движения применяется к странице", async () => {
    const { container } = render(<StyleguidePage />);
    await userEvent.click(screen.getByRole("radio", { name: /Тёмная/ }));
    expect(themePreference.read()).toBe("dark");

    await userEvent.click(screen.getByRole("radio", { name: "ЦУ" }));
    expect(container.querySelector("main")).toHaveAttribute("data-space", "cu");

    await userEvent.click(screen.getByRole("radio", { name: "200 %" }));
    expect(document.documentElement.style.fontSize).toBe("200%");

    await userEvent.click(screen.getByRole("switch", { name: /Спокойное движение/ }));
    expect(motionPreference.read()).toBe("calm");
    expect(window.localStorage.getItem(STORAGE_KEYS.motion)).toBe("calm");
  });

  it("ширина 320 ограничивает содержимое", async () => {
    render(<StyleguidePage />);
    await userEvent.click(screen.getByRole("radio", { name: "320" }));
    expect(
      screen.getByRole("heading", { level: 2, name: "Палитра" }).closest(".max-w-\\[20rem\\]"),
    ).not.toBeNull();
  });
});

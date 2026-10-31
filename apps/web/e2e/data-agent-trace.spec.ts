import { expect, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

/**
 * Экран трассы хода агента v4: сводка, шаги с вызовами инструментов, ошибки, лимиты и пауза,
 * страницы шагов, честное отсутствие API на движке v3. Данные — из MSW-набора по адресу `?run=`.
 */
test.describe("трасса хода агента", () => {
  test("завершённый ход: состояние, задача, шаги, артефакты и подробности", async ({ page }) => {
    await open(page, "/agent/runs/run-answer", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Трасса агента" })).toBeVisible();
    await expect(page.getByText("Завершено")).toBeVisible();
    await expect(page.getByText(/Разбери расписание на неделю/)).toBeVisible();
    await expect(page.getByText("4 322 токена")).toBeVisible();

    const steps = page.getByRole("list", { name: "Шаги трассы" });
    await expect(steps.getByRole("listitem")).toHaveCount(6);
    await expect(steps.getByText("Вызов инструмента: sources_sync")).toBeVisible();
    await expect(steps.getByText("Результат инструмента: sources_sync")).toBeVisible();
    await expect(steps.getByText("Итог")).toBeVisible();

    await expect(page.getByText("Артефакты")).toBeVisible();
    await expect(page.getByText("artifact-schedule-1")).toBeVisible();
    // Процентов и придуманного ETA нет.
    await expect(page.getByText(/\d+\s*%/)).toHaveCount(0);

    const modelCard = steps.getByRole("listitem").filter({ hasText: "Ответ модели" }).first();
    const toggle = modelCard.getByRole("button", { name: "Показать подробности" });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(modelCard.getByText("Текст модели")).toBeVisible();
    await expect(modelCard.getByText(/Посмотрю расписание/)).toBeVisible();
  });

  test("идущий ход: рельс без процентов и честная подпись о новых шагах", async ({ page }) => {
    await open(page, "/agent/runs/run-running", "connected");
    await expect(page.getByText("Выполняется")).toBeVisible();
    await expect(page.locator('[data-part="run-live"]')).toBeVisible();
    await expect(page.getByText("Новые шаги появятся здесь сами.")).toBeVisible();
  });

  test("лимит: остановка словами и настоящие числа из трассы", async ({ page }) => {
    await open(page, "/agent/runs/run-limit", "connected");
    await expect(page.getByText("Остановлено лимитом шагов", { exact: true }).first()).toBeVisible();
    const note = page.locator('[data-part="run-limit"]');
    await expect(note).toContainText("Ход остановлен: шаги");
    await expect(note).toContainText("шагов: 40");
  });

  test("пауза: ход остановлен, об этом сказано словами", async ({ page }) => {
    await open(page, "/agent/runs/run-paused", "connected");
    await expect(page.getByText("Пауза", { exact: true }).first()).toBeVisible();
    await expect(page.locator('[data-part="run-paused"]')).toContainText("Ход на паузе");
  });

  test("сбой: человеческий текст и код отдельно", async ({ page }) => {
    await open(page, "/agent/runs/run-error", "connected");
    await expect(page.getByText("Сбой", { exact: true }).first()).toBeVisible();
    const note = page.locator('[data-part="run-error"]');
    await expect(note).toContainText("Провайдер модели завершился ошибкой.");
    await expect(note).toContainText("llm_error");
  });

  test("движок v3: трассы нет — честный экран, а не бесконечная загрузка", async ({ page }) => {
    await open(page, "/agent/runs/run-answer?run=unavailable", "connected");
    await expect(page.getByText("Трассы на этом сервере нет")).toBeVisible();
    await expect(page.locator('[data-screen="agent-run"]')).toHaveAttribute("data-state", "empty");
  });

  test("шаги страницами: «Показать ещё» догружает остаток", async ({ page }) => {
    await open(page, "/agent/runs/run-long", "connected");
    const steps = page.getByRole("list", { name: "Шаги трассы" });
    await expect(steps.getByRole("listitem")).toHaveCount(100);
    await page.getByRole("button", { name: "Показать ещё" }).click();
    await expect(steps.getByRole("listitem")).toHaveCount(107);
    await expect(page.getByRole("button", { name: "Показать ещё" })).toHaveCount(0);
  });

  test("390 px: без горизонтальной прокрутки, зоны нажатия и текст по гайду", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, "/agent/runs/run-answer", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Трасса агента" })).toBeVisible();
    await expect(page.getByRole("list", { name: "Шаги трассы" }).getByRole("listitem")).toHaveCount(6);
    await expectFitsWidth(page, 390, "трасса агента");
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
  });

  test("a11y: один h1, список шагов с именем, доступное раскрытие подробностей", async ({ page }) => {
    await open(page, "/agent/runs/run-answer", "connected");
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
    const steps = page.getByRole("list", { name: "Шаги трассы" });
    await expect(steps).toBeVisible();
    await expect(steps.getByRole("listitem")).toHaveCount(6);
    await expect(steps.getByRole("button", { name: "Показать подробности" }).first()).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});

import { expect, type Page, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

const isLaptop = (page: Page) => (page.viewportSize()?.width ?? 0) >= 1024;

test.describe("«Расписание»", () => {
  test.beforeEach(async ({ page }) => freezeClock(page));

  test("на телефоне — лента дней и список дня, на ноутбуке — неделя сеткой в семь колонок", async ({
    page,
  }) => {
    await open(page, "/schedule", "connected");
    await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "ok");
    if (isLaptop(page)) {
      const grid = page.getByRole("region", { name: "Неделя" });
      await expect(grid.getByRole("heading", { level: 3 })).toHaveCount(7);
      await expect(page.getByRole("navigation", { name: "Дни недели" })).toHaveCount(0);
      const columns = await grid
        .locator(":scope > div")
        .evaluateAll((nodes) => nodes.map((node) => Math.round(node.getBoundingClientRect().top)));
      expect(new Set(columns).size).toBe(1);
    } else {
      await expect(page.getByRole("navigation", { name: "Дни недели" }).getByRole("button")).toHaveCount(7);
      await expect(page.getByRole("region", { name: /^вторник/ }).getByRole("listitem")).toHaveCount(4);
    }
  });

  test("пересечение пар разных вузов видно в полосе недели и на парах", async ({ page }) => {
    await open(page, "/schedule", "connected");
    await expect(page.getByText("4 пары пересекаются по времени с парами другого вуза")).toBeVisible();
    if (isLaptop(page)) await expect(page.getByText("Пересечение", { exact: true })).toHaveCount(4);
    else await expect(page.getByText("Пересекается по времени")).toHaveCount(2);
  });

  test("недели листаются кнопками; «Сегодня» возвращает", async ({ page }) => {
    await open(page, "/schedule", "connected");
    const range = page.getByText(/^\d+ \S+ – \d+ \S+$/);
    const first = await range.textContent();
    await page.getByRole("button", { name: "Следующая неделя" }).click();
    await expect(range).not.toHaveText(first ?? "");
    await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "ok");
    await page.getByRole("button", { name: "Сегодня" }).click();
    await expect(range).toHaveText(first ?? "");
  });

  test("фильтр по вузу из шапки оставляет пары одного вуза", async ({ page }) => {
    await open(page, "/schedule", "connected");
    await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "ok");
    await page.getByRole("button", { name: /Все вузы/ }).click();
    await page
      .getByRole("dialog", { name: "Вуз" })
      .getByRole("button", { name: /Центральный университет/ })
      .click();
    await expect(page.getByText("Основы продуктового менеджмента").first()).toBeVisible();
    await expect(page.getByText("Математический анализ")).toHaveCount(0);
    // Пересечение в отфильтрованной неделе по-прежнему помечено: партнёр — в другом вузе.
    await expect(page.getByText(/пересекаются по времени с парами другого вуза/)).toBeVisible();
  });

  test("карточка пары: источник со временем получения и пересекающаяся пара другого вуза", async ({
    page,
  }) => {
    await open(page, "/schedule", "connected");
    await page
      .getByRole("button", { name: /Алгоритмы и структуры данных/ })
      .first()
      .click();
    const sheet = page.getByRole("dialog", { name: "Алгоритмы и структуры данных" });
    await expect(sheet.getByText(/Основы продуктового менеджмента.*ЦУ/)).toBeVisible();
    await expect(sheet.getByText(/Получено/)).toBeVisible();
  });

  test("нужен повторный вход: полоса «Войти снова», расписание на месте", async ({ page }) => {
    await open(page, "/schedule", "reauth");
    await expect(page.getByRole("button", { name: "Войти снова" })).toBeVisible();
    await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "reauth");
    await expect(page.getByText("Основы продуктового менеджмента").first()).toBeVisible();
  });

  test("нет данных: «Пар нет» либо «На этой неделе пар нет», состояние empty", async ({ page }) => {
    await open(page, "/schedule?data=quiet", "connected");
    await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "empty");
    await expect(page.getByText(isLaptop(page) ? "На этой неделе пар нет" : "Пар нет")).toBeVisible();
  });

  test("нет сети: последний снимок и подпись про снимок", async ({ page, context }) => {
    await open(page, "/schedule", "connected");
    await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "ok");
    await context.setOffline(true);
    await expect(page.getByText(/Нет сети\. Показан снимок от/).first()).toBeVisible();
    await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "offline");
    await expect(page.getByText("Математический анализ").first()).toBeVisible();
  });

  test("зоны нажатия не меньше 44 px, текст не мельче 13 px, без горизонтальной прокрутки на 320/390/430", async ({
    page,
  }) => {
    await open(page, "/schedule", "connected");
    await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "ok");
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
    if (isLaptop(page)) return;
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await open(page, "/schedule", "connected");
      await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "ok");
      await expectFitsWidth(page, width, `расписание ${width}`);
    }
  });
});

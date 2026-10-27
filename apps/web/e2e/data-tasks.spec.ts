import { expect, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

const screen = (page: import("@playwright/test").Page) => page.locator("[data-screen=tasks]");

test.describe("«Задания»", () => {
  test.beforeEach(async ({ page }) => freezeClock(page));

  test("группы по порядку, срочность словами, задание без срока — «Срок не указан»", async ({ page }) => {
    await open(page, "/tasks", "connected");
    await expect(screen(page)).toHaveAttribute("data-state", "ok");
    const headings = await page.getByRole("heading", { level: 2 }).allTextContents();
    expect(headings).toEqual(["Просрочено2", "Сегодня1", "На неделе2", "Позже2", "Без срока1", "Сдано2"]);
    await expect(page.getByRole("region", { name: /^Сегодня/ }).getByText(/Осталось\s+5/)).toBeVisible();
    await expect(page.getByRole("region", { name: /^На неделе/ }).getByText(/Скоро · 2/)).toBeVisible();
    const none = page.getByRole("region", { name: /^Без срока/ });
    await expect(none.getByText("Срок не указан")).toBeVisible();
    await expect(none.getByText(/Осталось|Скоро|Просрочено/)).toHaveCount(0);
  });

  test("«Сдано» свёрнуто и раскрывается по кнопке, сданное подписано", async ({ page }) => {
    await open(page, "/tasks", "connected");
    const done = page.getByRole("region", { name: /^Сдано/ });
    await expect(done.getByText("Домашнее задание №3: пределы")).toHaveCount(0);
    await done.getByRole("button", { name: /Показать/ }).click();
    await expect(done.getByText("Домашнее задание №3: пределы")).toBeVisible();
    await expect(done.getByText("Сдано", { exact: true }).first()).toBeVisible();
  });

  test("карточка задания: срок, источник со временем получения, ссылка на условие", async ({ page }) => {
    await open(page, "/tasks", "connected");
    await page.getByRole("button", { name: /Лабораторная: алгоритмы сортировки/ }).click();
    const sheet = page.getByRole("dialog", { name: "Лабораторная: алгоритмы сортировки" });
    await expect(sheet.getByText("Срок по плану курса")).toBeVisible();
    await expect(sheet.getByText(/Получено/)).toBeVisible();
    await expect(sheet.getByRole("link", { name: /Открыть задание/ })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
  });

  test("фильтр по вузу из шапки: остаются задания одного вуза", async ({ page }) => {
    await open(page, "/tasks", "connected");
    await expect(screen(page)).toHaveAttribute("data-state", "ok");
    await page.getByRole("button", { name: /Все вузы/ }).click();
    await page
      .getByRole("dialog", { name: "Вуз" })
      .getByRole("button", { name: /Центральный университет/ })
      .click();
    await expect(page.getByText("Эссе по продуктовой стратегии")).toBeVisible();
    await expect(page.getByText("Курсовая: выбор темы")).toHaveCount(0);
  });

  test("нет данных: «Заданий пока нет», состояние empty, без «всё сдано»", async ({ page }) => {
    await open(page, "/tasks?data=quiet", "connected");
    await expect(page.getByText("Заданий пока нет")).toBeVisible();
    await expect(screen(page)).toHaveAttribute("data-state", "empty");
    await expect(page.getByText(/сдано/i)).toHaveCount(0);
  });

  test("нужен вход: полоса «Войти снова», задания на месте", async ({ page }) => {
    await open(page, "/tasks", "reauth");
    await expect(page.getByRole("button", { name: "Войти снова" })).toBeVisible();
    await expect(screen(page)).toHaveAttribute("data-state", "reauth");
    await expect(page.getByText("Эссе по продуктовой стратегии")).toBeVisible();
  });

  test("нет сети: последний снимок и подпись про снимок, кнопок повтора нет", async ({ page, context }) => {
    await open(page, "/tasks", "connected");
    await expect(screen(page)).toHaveAttribute("data-state", "ok");
    await context.setOffline(true);
    await expect(page.getByText(/Нет сети\. Показан снимок от/).first()).toBeVisible();
    await expect(screen(page)).toHaveAttribute("data-state", "offline");
    await expect(page.getByText("Эссе по продуктовой стратегии")).toBeVisible();
    await expect(page.getByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toHaveCount(0);
  });

  test("зоны нажатия, кегль и отсутствие горизонтальной прокрутки на 320/390/430", async ({ page }) => {
    await open(page, "/tasks", "connected");
    await expect(screen(page)).toHaveAttribute("data-state", "ok");
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
    if ((page.viewportSize()?.width ?? 0) >= 1024) return;
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await open(page, "/tasks", "connected");
      await expect(screen(page)).toHaveAttribute("data-state", "ok");
      await expectFitsWidth(page, width, `задания ${width}`);
    }
  });
});

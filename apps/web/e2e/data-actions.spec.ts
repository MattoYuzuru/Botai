import { expect, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const journal = (page: import("@playwright/test").Page) =>
  page.getByRole("list", { name: "Журнал действий" });

/** Автономия и журнал действий (docs/v4/19-actions.md): настройка режимов и решение по ask. */
test.describe("действия агента", () => {
  test("автономия в карточке источника: настройка режима и сброс к умолчанию", async ({ page }) => {
    await open(page, "/study", "connected");
    await page
      .getByRole("region", { name: /^Высшая школа экономики/ })
      .getByRole("button", { name: /^Личный кабинет:/ })
      .click();
    const card = page.getByRole("dialog", { name: "Личный кабинет" });
    const autonomy = card.getByRole("region", { name: "Автономия" });
    await expect(autonomy).toBeVisible();
    await expect(autonomy.locator("[data-policy-class]")).toHaveCount(5);
    await expect(autonomy.getByRole("combobox", { name: "Изменения: режим", exact: true })).toContainText(
      "Спрашивать",
    );

    await autonomy.getByRole("combobox", { name: "Изменения: режим", exact: true }).click();
    await page.getByRole("option", { name: "Автоматически" }).click();
    await expect(page.getByText("Настройка автономии сохранена")).toBeVisible();
    await expect(autonomy.getByText("своя настройка").first()).toBeVisible();
    await expect(autonomy.getByRole("combobox", { name: "Изменения: режим", exact: true })).toContainText(
      "Автоматически",
    );

    await autonomy.getByRole("button", { name: "Вернуть по умолчанию: Изменения", exact: true }).click();
    await expect(page.getByText("Настройка сброшена к значению по умолчанию")).toBeVisible();
    await expect(autonomy.getByRole("combobox", { name: "Изменения: режим", exact: true })).toContainText(
      "Спрашивать",
    );
  });

  test("журнал: записи, решения и фильтр «ждут решения»", async ({ page }) => {
    await open(page, "/actions", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Действия агента" })).toBeVisible();
    await expect(journal(page).getByRole("listitem")).toHaveCount(5);
    await expect(journal(page).getByText("Записаться на пересдачу по алгоритмам")).toBeVisible();
    await expect(journal(page).getByText("Ждёт решения")).toBeVisible();
    // Область подписана названием источника, если сервер его дал.
    await expect(journal(page).getByText("Личный кабинет").first()).toBeVisible();
    await expect(journal(page).getByText("Выполнено", { exact: true })).toBeVisible();

    await page.getByRole("combobox", { name: "Решение", exact: true }).click();
    await page.getByRole("option", { name: "Ждут решения" }).click();
    await expect(journal(page).getByRole("listitem")).toHaveCount(1);
  });

  test("фильтр по источнику: из журнала и сброс ко всем источникам", async ({ page }) => {
    await open(page, "/actions", "connected");
    await expect(journal(page).getByRole("listitem")).toHaveCount(5);
    await page.getByRole("combobox", { name: "Источник", exact: true }).click();
    await page.getByRole("option", { name: "Личный кабинет", exact: true }).click();
    await expect(journal(page).getByRole("listitem")).toHaveCount(4);
    await page.getByRole("combobox", { name: "Источник", exact: true }).click();
    await page.getByRole("option", { name: "Все источники" }).click();
    await expect(journal(page).getByRole("listitem")).toHaveCount(5);
  });

  test("«Разрешить» записывает решение и обновляет запись", async ({ page }) => {
    await open(page, "/actions", "connected");
    await journal(page)
      .getByRole("button", { name: /^Записаться на пересдачу/ })
      .click();
    const sheet = page.getByRole("dialog", { name: "Записаться на пересдачу по алгоритмам" });
    await expect(sheet.getByText(/Решение запишется в журнал/)).toBeVisible();
    await expect(sheet.getByText(/POST https:\/\/lk\.fakeuni\.test\/api\/requests/)).toBeVisible();
    await expect(sheet.getByRole("link", { name: "Открыть трассу хода" })).toHaveAttribute(
      "href",
      "/agent/runs/run-answer",
    );
    await sheet.getByRole("button", { name: "Разрешить" }).click();
    await expect(page.getByText("Действие разрешено")).toBeVisible();
    await expect(sheet.getByText("Разрешено")).toBeVisible();
    await expect(sheet.getByRole("button", { name: "Разрешить" })).toHaveCount(0);
  });

  test("«Запретить» просит подтверждения и закрывает действие", async ({ page }) => {
    await open(page, "/actions", "connected");
    await journal(page)
      .getByRole("button", { name: /^Записаться на пересдачу/ })
      .click();
    const sheet = page.getByRole("dialog", { name: "Записаться на пересдачу по алгоритмам" });
    await sheet.getByRole("button", { name: "Запретить" }).click();
    const dialog = page.getByRole("alertdialog", { name: "Запретить действие?" });
    await dialog.getByRole("button", { name: "Запретить" }).click();
    await expect(page.getByText("Действие запрещено")).toBeVisible();
    await expect(sheet.getByText("Запрещено владельцем")).toBeVisible();
  });

  test("пусто и ошибка: честные состояния журнала", async ({ page }) => {
    await open(page, "/actions?actions=empty", "connected");
    await expect(page.getByText("Действий пока нет")).toBeVisible();
    await open(page, "/actions?actions=fail", "connected");
    await expect(page.getByText(/Журнал не загрузился/)).toBeVisible();
    await expect(page.getByText("Действий пока нет")).toHaveCount(0);
  });

  test("390 px: журнал без горизонтальной прокрутки, зоны нажатия и текст по гайду", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, "/actions", "connected");
    await expect(journal(page).getByRole("listitem")).toHaveCount(5);
    await expectFitsWidth(page, 390, "журнал действий");
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);

    await page.getByRole("button", { name: /^Записаться на пересдачу/ }).click();
    await expect(page.getByRole("dialog", { name: "Записаться на пересдачу по алгоритмам" })).toBeVisible();
    await expectFitsWidth(page, 390, "карточка действия");
    expect(await findSmallTapTargets(page, '[role="dialog"]')).toEqual([]);
  });

  test("a11y: один h1, журнал списком, фильтры с подписями", async ({ page }) => {
    await open(page, "/actions", "connected");
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
    await expect(journal(page)).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Решение" })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Класс действия" })).toBeVisible();
  });
});

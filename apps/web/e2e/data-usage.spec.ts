import { expect, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const usage = (page: import("@playwright/test").Page) =>
  page.getByRole("region", { name: "Расход и лимиты" });

async function openUsage(page: import("@playwright/test").Page, query = "") {
  // Настройки живут в разделе «Источники» (D-046).
  await open(page, `/study?view=sources${query.replace("?", "&")}`, "connected");
  await page.getByRole("heading", { name: "Расход и лимиты" }).scrollIntoViewIfNeeded();
  return usage(page);
}

/**
 * «Расход и лимиты» в настройках (usage/router.py): настоящее число токенов и лимит словами,
 * семь дней с токенами и стоимостью, владельцу — расход общего ключа. Без процентов и прогнозов.
 */
test.describe("расход и лимиты", () => {
  test("сегодня, лимит и последние дни — настоящие числа", async ({ page }) => {
    const region = await openUsage(page);
    await expect(region.getByText(/41\s+250\s+токенов\s+из\s+1\s+000\s+000\s+токенов/)).toBeVisible();
    await expect(region.getByText(/Лимит сбросится/)).toBeVisible();
    const days = region.getByRole("list", { name: "Расход по дням" });
    await expect(days.getByRole("listitem")).toHaveCount(7);
    await expect(region.getByText(/За 7\s+дней/)).toBeVisible();
    await expect(page.getByText(/\d+\s*%/)).toHaveCount(0);
  });

  test("владелец видит расход общего ключа по людям", async ({ page }) => {
    const region = await openUsage(page, "?owner=1");
    await expect(region.getByText("Общий ключ")).toBeVisible();
    const users = region.getByRole("list", { name: "Расход по людям" });
    await expect(users.getByRole("listitem")).toHaveCount(2);
    await expect(users.getByText("Мария")).toBeVisible();
    await expect(region.getByText(/Всего сегодня:/)).toBeVisible();
  });

  test("не владелец не видит общий ключ", async ({ page }) => {
    await openUsage(page);
    await expect(page.getByText("Общий ключ")).toHaveCount(0);
  });

  test("пусто и ошибка: честные состояния вместо нулей или процентов", async ({ page }) => {
    const empty = await openUsage(page, "?usage=empty");
    await expect(empty.getByText(/За последние дни расхода не было/)).toBeVisible();
    await expect(empty.getByRole("list", { name: "Расход по дням" })).toHaveCount(0);
    const failed = await openUsage(page, "?usage=fail");
    await expect(failed.getByText(/Расход не загрузился/)).toBeVisible();
    await expect(failed.getByText(/За последние дни расхода не было/)).toHaveCount(0);
  });

  test("390 px: карточка без горизонтальной прокрутки, зоны нажатия по гайду", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const region = await openUsage(page);
    await expect(region.getByRole("list", { name: "Расход по дням" })).toBeVisible();
    await expectFitsWidth(page, 390, "расход и лимиты");
    expect(await findSmallTapTargets(page, 'section[aria-labelledby="usage-title"]')).toEqual([]);
  });

  test("a11y: именованный регион и списки", async ({ page }) => {
    const region = await openUsage(page);
    await expect(region.getByRole("heading", { level: 2, name: "Расход и лимиты" })).toBeVisible();
    await expect(region.getByRole("list", { name: "Расход по дням" })).toBeVisible();
  });
});

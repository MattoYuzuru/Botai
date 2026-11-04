import { expect, type Page, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const panels = (page: Page) => page.locator("[data-panels=ready]");
const ranking = (page: Page) => page.locator('[data-panel-type="ranking"]');

/** Выбор вуза в переключателе шапки: «Все вузы» -> вуз и обратно. */
async function pickUniversity(page: Page, current: RegExp, next: RegExp) {
  await page.getByRole("button", { name: current }).first().click();
  await page.getByRole("dialog", { name: "Вуз" }).getByRole("button", { name: next }).click();
}

/**
 * «Успеваемость» в «Учёбе» (D-039): панели из `GET /api/v1/panels` на моках, без вычислений на клиенте.
 * Идёт на телефоне (390 px) и ноутбуке (1280 px): раскладка разная, правила доступности общие.
 */
test.describe("«Учёба»: панели успеваемости", () => {
  test("блок над вузами: группы по вузам, показатели, график, рейтинг, профиль; без горизонтальной прокрутки", async ({
    page,
  }) => {
    await open(page, "/study", "connected");
    await expect(page.getByRole("heading", { level: 2, name: "Успеваемость" })).toBeVisible();
    await expect(panels(page)).toBeVisible();

    const groups = panels(page).getByRole("group", {
      name: /Высшая школа экономики|Центральный университет/,
    });
    await expect(groups).toHaveCount(2);
    const hse = groups.first();
    await expect(hse.getByRole("group", { name: "Последняя оценка" })).toContainText("из 10");
    await expect(hse.getByText("Шкала источника: 10 баллов").first()).toBeVisible();
    await expect(hse.getByText("Вы: 12 из 140")).toBeVisible();
    await expect(hse.getByText("Анна Тестова")).toBeVisible();
    // Блок идёт выше карточек вузов и источников.
    const panelsTop = (await panels(page).boundingBox())?.y ?? 0;
    const sourcesTop =
      (await page.getByRole("region", { name: /^Высшая школа экономики/ }).boundingBox())?.y ?? 0;
    expect(panelsTop).toBeLessThan(sourcesTop);

    const width = page.viewportSize()?.width ?? 0;
    await expectFitsWidth(page, width, "панели «Успеваемости»");
    expect(await findTinyText(page)).toEqual([]);
    expect(await findSmallTapTargets(page, "[data-panels=ready]")).toEqual([]);
    // Никаких средних и процентов: приложение ничего не считает за источник.
    await expect(panels(page)).not.toContainText(/средн|\d\s*%/i);
  });

  test("на телефоне показатели по две в ряд, панели одной колонкой", async ({ page }) => {
    test.skip((page.viewportSize()?.width ?? 0) > 500, "только телефон");
    await open(page, "/study", "connected");
    const tiles = panels(page).getByRole("group", { name: "Показатели" }).first().locator("[data-stat-tile]");
    await expect(tiles).toHaveCount(3);
    const boxes = await tiles.evaluateAll((nodes) =>
      nodes.map((node) => {
        const box = node.getBoundingClientRect();
        return { top: Math.round(box.top), left: Math.round(box.left), width: Math.round(box.width) };
      }),
    );
    expect(boxes[1]?.top).toBe(boxes[0]?.top);
    expect(boxes[2]?.top).toBeGreaterThan(boxes[0]?.top ?? 0);
    expect(boxes[2]?.width).toBeGreaterThan((boxes[0]?.width ?? 0) * 1.5);
  });

  test("на ноутбуке боковая колонка стоит справа от графика", async ({ page }) => {
    test.skip((page.viewportSize()?.width ?? 0) < 1024, "только ноутбук");
    await open(page, "/study", "connected");
    const chart = page.locator('[data-panel-type="chart"]').first();
    const rating = ranking(page).first();
    const [chartBox, ratingBox] = [await chart.boundingBox(), await rating.boundingBox()];
    expect(ratingBox?.x ?? 0).toBeGreaterThan((chartBox?.x ?? 0) + (chartBox?.width ?? 0) - 1);
    expect(Math.abs((ratingBox?.y ?? 0) - (chartBox?.y ?? 0))).toBeLessThan(2);
  });

  test("переключатель вуза: один вуз — только его панели, без групп; «Все вузы» — группы снова", async ({
    page,
  }) => {
    await open(page, "/study", "connected");
    await expect(panels(page).getByRole("group", { name: /Центральный университет/ })).toBeVisible();
    await pickUniversity(page, /Все вузы/, /Центральный университет/);
    await expect(panels(page).getByRole("group", { name: /Высшая школа экономики/ })).toHaveCount(0);
    await expect(ranking(page)).toHaveCount(0);
    await expect(panels(page).getByText("Шкала источника: 100 баллов")).toBeVisible();

    await pickUniversity(page, /ЦУ|Центральный/, /Высшая школа экономики/);
    await expect(panels(page).getByRole("heading", { level: 3, name: "Рейтинг потока" })).toBeVisible();
    // Панель, скрытая в «Все вузы», в режиме одного вуза возвращается.
    await expect(panels(page).getByRole("heading", { level: 3, name: "Последние оценки" })).toBeVisible();

    await pickUniversity(page, /ВШЭ|Высшая/, /Все вузы/);
    await expect(panels(page).getByRole("group", { name: /Высшая школа экономики/ })).toBeVisible();
    await expect(panels(page).getByRole("heading", { name: "Последние оценки" })).toHaveCount(0);
  });

  test("«Настроить»: выключить рейтинг, скрыть профиль в «Все вузы», вернуть обратно", async ({ page }) => {
    await open(page, "/study", "connected");
    await expect(ranking(page)).toHaveCount(1);
    await page.getByRole("button", { name: "Настроить" }).click();
    const sheet = page.getByRole("dialog", { name: "Панели успеваемости" });
    await expect(sheet).toBeVisible();
    const group = (title: string) => sheet.getByRole("group", { name: new RegExp(`^${title}`) }).first();

    await group("Рейтинг потока").getByRole("switch", { name: "Показывать", exact: true }).click();
    await expect(ranking(page)).toHaveCount(0);
    // Выключенная панель остаётся в настройках, второй переключатель при этом недоступен.
    await expect(
      group("Рейтинг потока").getByRole("switch", { name: "Показывать", exact: true }),
    ).not.toBeChecked();
    await expect(group("Рейтинг потока").getByRole("switch", { name: /Все вузы/ })).toBeDisabled();

    await group("Профиль")
      .getByRole("switch", { name: /Все вузы/ })
      .click();
    await expect(page.locator('[data-panel-type="profile"]')).toHaveCount(0);

    await group("Рейтинг потока").getByRole("switch", { name: "Показывать", exact: true }).click();
    await expect(ranking(page)).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);
  });

  test("предложение агента: предпросмотр, «Добавить» делает панель настоящей", async ({ page }) => {
    await open(page, "/study", "connected");
    await pickUniversity(page, /Все вузы/, /Высшая школа экономики/);
    const card = page.getByRole("article", {
      name: /Агент предлагает добавить панель «Оценки по предметам»/,
    });
    await expect(card).toBeVisible();
    await expect(card.getByRole("img", { name: /Предпросмотр панели/ })).toBeVisible();
    await card.getByRole("button", { name: /Добавить панель/ }).click();
    await expect(card).toHaveCount(0);
    await expect(panels(page).getByRole("heading", { level: 3, name: "Оценки по предметам" })).toBeVisible();
  });

  test("сворачивание панели запоминается после перезагрузки", async ({ page }) => {
    await open(page, "/study", "connected");
    await pickUniversity(page, /Все вузы/, /Высшая школа экономики/);
    const toggle = panels(page)
      .getByRole("heading", { level: 3, name: "Рейтинг потока" })
      .getByRole("button");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await page.reload();
    await expect(
      panels(page).getByRole("heading", { level: 3, name: "Рейтинг потока" }).getByRole("button"),
    ).toHaveAttribute("aria-expanded", "false");
  });

  test("нет панелей — блока нет, «Учёба» работает; сбой — спокойная строка", async ({ page }) => {
    await open(page, "/study?panels=none", "connected");
    await expect(page.getByRole("region", { name: /^Высшая школа экономики/ })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "Успеваемость" })).toHaveCount(0);

    await open(page, "/study?panels=fail", "connected");
    await expect(page.getByText(/Панели успеваемости не загрузились/)).toBeVisible();
    await expect(page.getByRole("region", { name: /^Высшая школа экономики/ })).toBeVisible();
  });

  test("панель неизвестного типа скрыта, остальные работают", async ({ page }) => {
    await open(page, "/study?panels=unknown", "connected");
    await expect(panels(page)).toBeVisible();
    await expect(page.locator('[data-panel-type="heatmap"]')).toHaveCount(0);
    await expect(ranking(page)).toHaveCount(1);
  });
});

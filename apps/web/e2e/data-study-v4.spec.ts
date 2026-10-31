import { expect, type Page, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const sourcesOf = (page: Page, university: RegExp) =>
  page.getByRole("region", { name: university }).getByRole("list", { name: /^Источники:/ });

/**
 * Новый UI «Учёбы» (03-sources-and-ux.md §5, docs/v4/17-study-ui.md) за флагом `?studyUi=v4`.
 * В мок-режиме `/api/v1/sources` ещё не отвечает (эндпоинта нет в MSW-наборе), поэтому экран
 * проверяется на fallback к старым подключениям — то же поведение, что на бэкенде v3, и оно обязано
 * быть бесшовным. Полный путь нового API — в Vitest-тестах (`features/study/v4/study-screen-v4.test.tsx`).
 */
test.describe("«Учёба» v4: источники университета", () => {
  test("вуз — карточка, источники со статусами, документами и без «Моих материалов»", async ({ page }) => {
    await open(page, "/study?studyUi=v4", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Учёба" })).toBeVisible();

    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    const sources = sourcesOf(page, /^Высшая школа экономики/);
    await expect(sources.getByRole("listitem")).toHaveCount(2);
    await expect(sources.getByText("Личный кабинет").first()).toBeVisible();
    await expect(sources.getByText("Расписание (календарь)")).toBeVisible();
    await expect(sources.getByText("Работает").first()).toBeVisible();
    await expect(sources.getByText(/Обновлено/).first()).toBeVisible();
    expect(await hse.getAttribute("data-source-mode")).toBe("connections");

    // Документы — в карточке вуза; отдельных мест «Документы и вложения» и «Источники» нет.
    await expect(hse.getByText("Документы", { exact: true })).toBeVisible();
    await expect(hse.getByText("Программа курса: математический анализ.pdf")).toBeVisible();
    await expect(page.getByRole("link", { name: /Документы и вложения/ })).toHaveCount(0);
    await expect(page.getByText(/Мои материалы/)).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Источники", exact: true })).toHaveCount(0);

    // «Добавить источник» и «Спросить агента» ведут в тот же агентский тред с готовой фразой.
    await expect(hse.getByRole("link", { name: "Добавить источник" })).toHaveAttribute(
      "href",
      /\/chat\?draft=/,
    );
    await hse.getByRole("link", { name: "Добавить источник" }).click();
    await expect(page).toHaveURL(/\/chat\?draft=/);
    expect(new URL(page.url()).searchParams.get("draft")).toContain("Высшая школа экономики");
  });

  test("карточка источника: что читает, когда обновлялся, «Открыть» и «Спросить агента»", async ({
    page,
  }) => {
    await open(page, "/study?studyUi=v4", "connected");
    const sources = sourcesOf(page, /^Высшая школа экономики/);
    await sources.getByRole("button", { name: /^Личный кабинет: Работает/ }).click();

    const sheet = page.getByRole("dialog", { name: "Личный кабинет" });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByText("Последняя синхронизация")).toBeVisible();
    await expect(sheet.getByRole("link", { name: "Открыть сайт: Личный кабинет" })).toHaveAttribute(
      "href",
      "https://lk.fakeuni.test/",
    );
    await expect(sheet.getByRole("link", { name: "Спросить агента: Личный кабинет" })).toHaveAttribute(
      "href",
      /\/chat\?draft=/,
    );
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);
  });

  test("флаг выключен: остаётся текущий экран «Учёбы»", async ({ page }) => {
    await open(page, "/study", "connected");
    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    await expect(hse.getByRole("link", { name: /Документы и вложения/ })).toBeVisible();
    await expect(page.locator("[data-source-mode]")).toHaveCount(0);
  });

  test("старые места «Источники» и «Документы и вложения» при v4 не открываются", async ({ page }) => {
    await open(page, "/study/connections?studyUi=v4", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Учёба" })).toBeVisible();
    await open(page, "/study/documents?studyUi=v4", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Учёба" })).toBeVisible();
  });

  test("мобильная вёрстка 390 px: без горизонтальной прокрутки, крупные зоны нажатия и текст", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, "/study?studyUi=v4", "connected");
    await expect(sourcesOf(page, /^Высшая школа экономики/).getByRole("listitem")).toHaveCount(2);
    await expectFitsWidth(page, 390, "Учёба v4");
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
  });
});

import { expect, type Page, test } from "@playwright/test";
import {
  expectFitsWidth,
  expectSheetReady,
  findSmallTapTargets,
  findTinyText,
  freezeClock,
  open,
} from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const sourcesOf = (page: Page, university: RegExp) =>
  page.getByRole("region", { name: university }).getByRole("list", { name: /^Источники:/ });

/**
 * «Учёба» — единственный экран источников (03-sources-and-ux.md §5, D-013): без фича-флага, на настоящем
 * v4-API из MSW (не на fallback). Проверяются вуз-карточка, источники со статусами, действия карточки,
 * пустота и ошибка, прежние адреса и мобильная доступность.
 */
test.describe("«Учёба»: источники вуза", () => {
  test("по умолчанию: вузы, источники со статусами, документы вуза и никаких «Моих материалов»", async ({
    page,
  }) => {
    await open(page, "/study", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Учёба" })).toBeVisible();

    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    const sources = sourcesOf(page, /^Высшая школа экономики/);
    await expect(sources.getByRole("listitem")).toHaveCount(2);
    await expect(sources.getByText("Личный кабинет")).toBeVisible();
    await expect(sources.getByText("Расписание (календарь)")).toBeVisible();
    await expect(sources.getByText("Работает").first()).toBeVisible();
    await expect(sources.getByText(/Обновлено/).first()).toBeVisible();
    await expect(sources.getByText("Основное расписание")).toBeVisible();
    await expect(sources.getByText("Дополнительное расписание")).toBeVisible();
    // Новый API, а не откат на прежние подключения.
    expect(await hse.getAttribute("data-source-mode")).toBe("v4");

    // Документы — в карточке вуза; отдельных мест и разделов нет.
    await expect(hse.getByText("Программа курса: математический анализ.pdf")).toBeVisible();
    await expect(page.getByRole("link", { name: /Документы и вложения/ })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Источники", exact: true })).toHaveCount(0);
    await expect(page.getByText(/Мои материалы/)).toHaveCount(0);

    // «Добавить источник» и «Спросить агента» — чат с готовой просьбой.
    await expect(hse.getByRole("link", { name: "Добавить источник" })).toHaveAttribute(
      "href",
      /\/chat\?draft=/,
    );
    await hse.getByRole("link", { name: "Добавить источник" }).click();
    await expect(page).toHaveURL(/\/chat\?draft=/);
    expect(new URL(page.url()).searchParams.get("draft")).toContain("Высшая школа экономики");
  });

  test("карточка источника: что читает, описание чтения, артефакты и действия", async ({ page }) => {
    await open(page, "/study", "connected");
    await sourcesOf(page, /^Высшая школа экономики/)
      .getByRole("button", { name: /^Личный кабинет:/ })
      .click();

    const sheet = page.getByRole("dialog", { name: "Личный кабинет" });
    await expectSheetReady(page, "Личный кабинет");
    await expect(sheet.getByText("Последняя синхронизация")).toBeVisible();
    await expect(sheet.getByText("Декларативное описание")).toBeVisible();
    await expect(sheet.getByText("Ревизия 2")).toBeVisible();
    await expect(sheet.getByText("Прочитанное")).toBeVisible();
    await expect(sheet.getByText("Сроки заданий")).toBeVisible();
    await expect(sheet.locator('[data-part="runs-placeholder"]')).toContainText(/История запусков/);
    await expect(sheet.getByRole("link", { name: "Открыть сайт: Личный кабинет" })).toHaveAttribute(
      "href",
      "https://lk.fakeuni.test/",
    );
    await expect(sheet.getByRole("link", { name: "Спросить агента: Личный кабинет" })).toHaveAttribute(
      "href",
      /\/chat\?draft=/,
    );
    // Кнопок «Обновить/Повторить/Загрузить» нет: только бизнес-действия.
    await expect(sheet.getByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);
  });

  test("«Перечитать»: прогон и честный итог, без выдуманных процентов", async ({ page }) => {
    await open(page, "/study", "connected");
    await sourcesOf(page, /^Высшая школа экономики/)
      .getByRole("button", { name: /^Личный кабинет:/ })
      .click();
    const sheet = page.getByRole("dialog", { name: "Личный кабинет" });
    await sheet.getByRole("button", { name: "Перечитать" }).click();
    await expect(page.getByText("Источник перечитан")).toBeVisible();
    await expect(page.getByText(/\d+\s*%/)).toHaveCount(0);
    await expect(sheet.getByText("Работает")).toBeVisible();
  });

  test("сломанный источник: «Починить» возвращает его в работу", async ({ page }) => {
    await open(page, "/study?data=error", "connected");
    const sources = sourcesOf(page, /^Высшая школа экономики/);
    await expect(sources.getByText("Ошибка")).toBeVisible();
    await sources.getByRole("button", { name: /^Личный кабинет:/ }).click();
    const sheet = page.getByRole("dialog", { name: "Личный кабинет" });
    await expect(sheet.getByText(/Сайт вуза не отвечает/)).toBeVisible();
    await sheet.getByRole("button", { name: "Починить" }).click();
    await expect(page.getByText("Источник обновлён после починки")).toBeVisible();
    await expect(sheet.getByText("Работает")).toBeVisible();
  });

  test("«Выключить источник» просит подтверждения; выключенный включается обратно", async ({ page }) => {
    await open(page, "/study", "connected");
    await sourcesOf(page, /^Высшая школа экономики/)
      .getByRole("button", { name: /^Личный кабинет:/ })
      .click();
    const sheet = page.getByRole("dialog", { name: "Личный кабинет" });
    await sheet.getByRole("button", { name: "Выключить источник" }).click();
    const dialog = page.getByRole("alertdialog", { name: "Выключить «Личный кабинет»?" });
    await expect(dialog.getByText(/Ничего не удаляется/)).toBeVisible();
    await dialog.getByRole("button", { name: "Выключить источник" }).click();
    await expect(page.getByText("Источник выключен")).toBeVisible();
    await expect(sheet.getByText("Выключен", { exact: true })).toBeVisible();
    await sheet.getByRole("button", { name: "Включить" }).click();
    await expect(page.getByText("Источник включён")).toBeVisible();
  });

  test("«Сделать основным для расписания» переносит роль на выбранный источник", async ({ page }) => {
    await open(page, "/study", "connected");
    const sources = sourcesOf(page, /^Высшая школа экономики/);
    await sources.getByRole("button", { name: /^Расписание \(календарь\):/ }).click();
    const sheet = page.getByRole("dialog", { name: "Расписание (календарь)" });
    await sheet.getByRole("button", { name: /Сделать основным для расписания/ }).click();
    await expect(page.getByText(/Основное расписание — «Расписание \(календарь\)»/)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(
      sources
        .getByRole("listitem")
        .filter({ hasText: "Расписание (календарь)" })
        .getByText("Основное расписание"),
    ).toBeVisible();
  });

  test("пусто: у вуза нет источников — честное состояние и «Добавить источник»", async ({ page }) => {
    await open(page, "/study?sources=none", "connected");
    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    await expect(hse.getByText(/У вуза пока нет источников/)).toBeVisible();
    await expect(hse.getByText("Источников пока нет")).toBeVisible();
    await expect(hse.getByRole("link", { name: "Добавить источник" })).toBeVisible();
  });

  test("настоящая ошибка загрузки видна, а не выдаётся за пустоту", async ({ page }) => {
    await open(page, "/study?sources=fail", "connected");
    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    await expect(hse.getByText(/Не удалось загрузить источники/)).toBeVisible();
    await expect(hse.getByText(/У вуза пока нет источников/)).toHaveCount(0);
  });

  test("нет сети: последние данные остаются, полоса честно говорит об офлайне", async ({ page, context }) => {
    await open(page, "/study", "connected");
    await expect(sourcesOf(page, /^Высшая школа экономики/).getByRole("listitem")).toHaveCount(2);
    await context.setOffline(true);
    await expect(page.getByText(/Нет сети\. Показаны последние данные/)).toBeVisible();
    await expect(sourcesOf(page, /^Высшая школа экономики/).getByRole("listitem")).toHaveCount(2);
  });

  test("прежние адреса «Источники» и «Документы и вложения» ведут на «Учёбу»", async ({ page }) => {
    await open(page, "/study/connections", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Учёба" })).toBeVisible();
    await open(page, "/study/documents", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Учёба" })).toBeVisible();
  });

  test("мобильная вёрстка 390 px: без горизонтальной прокрутки, зоны нажатия и текст по гайду", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, "/study", "connected");
    await expect(sourcesOf(page, /^Высшая школа экономики/).getByRole("listitem")).toHaveCount(2);
    await expectFitsWidth(page, 390, "Учёба");
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);

    // Карточка источника на телефоне: доступные роли и подписи, действия в листе.
    await sourcesOf(page, /^Высшая школа экономики/)
      .getByRole("button", { name: /^Личный кабинет:/ })
      .click();
    await expectSheetReady(page, "Личный кабинет");
    await expectFitsWidth(page, 390, "карточка источника");
    expect(await findSmallTapTargets(page, '[role="dialog"]'), "зоны нажатия в карточке").toEqual([]);
  });

  test("a11y: один h1, регионы вузов, список источников и доступные имена действий", async ({ page }) => {
    await open(page, "/study", "connected");
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
    await expect(page.getByRole("region").first()).toBeVisible();
    const list = sourcesOf(page, /^Высшая школа экономики/);
    await expect(list).toBeVisible();
    await expect(list.getByRole("button").first()).toHaveAttribute(
      "aria-label",
      /Работает|Нужен вход|Ошибка|Обновляется/,
    );
    await expect(page.getByRole("link", { name: "Добавить источник" }).first()).toBeVisible();
  });
});

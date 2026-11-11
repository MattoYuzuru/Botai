import { expect, type Page, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const card = (page: Page, course: string) => page.getByRole("button", { name: new RegExp(`^${course}\\.`) });
const wide = (page: Page) => (page.viewportSize()?.width ?? 0) >= 1024;

/**
 * «Учёба» → «Успеваемость» (D-046) на моках: предметы с итогом/накопленным как у источника, «Новое», зачётка,
 * показатели, лист предмета. Телефон 390 и ноутбук 1280.
 */
test.describe("«Учёба»: успеваемость", () => {
  test("открывается успеваемостью; первый экран телефона — данные, а не рамки", async ({ page }) => {
    await open(page, "/study", "connected");
    await expect(page.getByRole("radio", { name: "Успеваемость" })).toHaveAttribute("aria-checked", "true");
    await expect(page.getByRole("region", { name: "Новое" })).toBeVisible();
    await expect(card(page, "Математический анализ")).toBeVisible();
    // Хотя бы одна карточка предмета начинается на первом экране.
    const first = await page.locator("[data-course-card]").first().boundingBox();
    expect(first?.y ?? 9999).toBeLessThan(page.viewportSize()?.height ?? 0);
    await expect(page.getByRole("region", { name: "Предметы" })).not.toContainText(/средн|\d\s*%/i);
  });

  test("переключатель: раздел в адресе, переживает перезагрузку и «назад»", async ({ page }) => {
    await open(page, "/study", "connected");
    await page.getByRole("radio", { name: "Источники" }).click();
    await expect(page).toHaveURL(/view=sources/);
    await expect(page.getByRole("region", { name: /^Высшая школа экономики/ })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("radio", { name: "Источники" })).toHaveAttribute("aria-checked", "true");
    await page
      .getByRole("region", { name: /^Высшая школа экономики/ })
      .getByRole("link", { name: /Предметы/ })
      .click();
    await expect(page.getByRole("heading", { level: 1, name: "Предметы" })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("radio", { name: "Источники" })).toHaveAttribute("aria-checked", "true");
  });

  test("лист предмета: накопленное, работы, комментарий, ссылка на оценку; телефон — снизу, ноутбук — справа", async ({
    page,
  }) => {
    await open(page, "/study", "connected");
    await card(page, "Математический анализ").click();
    const sheet = page.getByRole("dialog", { name: "Математический анализ" });
    await expect(sheet.getByRole("region", { name: "Накоплено" })).toContainText("7,6");
    await expect(sheet.getByText("Комментарий проверяющего")).toBeVisible();
    await expect(sheet.getByRole("link", { name: "Открыть в источнике: Контрольная №2" })).toHaveAttribute(
      "href",
      /^https:\/\/lk\.fakeuni\.test\//,
    );
    const box = await sheet.boundingBox();
    if (wide(page)) expect(box?.x ?? 0).toBeGreaterThan(600);
    else expect(box?.width ?? 0).toBeGreaterThan(380);
    expect(await findSmallTapTargets(page, "[role=dialog] [data-grade-id]")).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
  });

  test("ноутбук: предметы сеткой слева, «Новое» справа; телефон — одна колонка", async ({ page }) => {
    await open(page, "/study", "connected");
    const courses = await page.getByRole("region", { name: "Предметы" }).boundingBox();
    const updates = await page.getByRole("region", { name: "Новое" }).boundingBox();
    if (!courses || !updates) throw new Error("нет блоков");
    if (wide(page)) {
      expect(updates.x).toBeGreaterThan(courses.x + courses.width - 1);
      const cards = page.locator("[data-course-card]");
      const [a, b] = [await cards.nth(0).boundingBox(), await cards.nth(1).boundingBox()];
      expect(Math.abs((a?.y ?? 0) - (b?.y ?? 1))).toBeLessThan(2);
    } else {
      expect(courses.y).toBeGreaterThan(updates.y + updates.height - 1);
    }
  });

  test("зачётка и показатели; без горизонтальной прокрутки, зоны нажатия и кегль по гайду", async ({
    page,
  }) => {
    await open(page, "/study", "connected");
    const book = page.getByRole("region", { name: "Зачётка" });
    await expect(book.getByRole("button", { name: /Линейная алгебра/ })).toContainText("9 из 10");
    await book.getByRole("button", { name: /Показать всю зачётку/ }).click();
    await expect(book.getByText(/2025\/26, осень/)).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Показатели" }).getByRole("group", { name: "GPA" }),
    ).toContainText("4,6");
    await expect(page.locator("[aria-busy=true]")).toHaveCount(0);
    expect(await findSmallTapTargets(page, "[data-view=performance]")).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
    await expectFitsWidth(page, page.viewportSize()?.width ?? 0, "успеваемость");
    if (!wide(page)) {
      await page.setViewportSize({ width: 320, height: 800 });
      await expectFitsWidth(page, 320, "успеваемость 320");
    }
  });

  test("пусто: без оценок и панелей экран открывается источниками, «Успеваемость» честно пуста", async ({
    page,
  }) => {
    await open(page, "/study?data=quiet&panels=none", "connected");
    await expect(page.getByRole("radio", { name: "Источники" })).toHaveAttribute("aria-checked", "true");
    await page.getByRole("radio", { name: "Успеваемость" }).click();
    await expect(page.getByRole("heading", { name: "Оценок пока нет" })).toBeVisible();
    await page.getByRole("link", { name: "Найти оценки" }).click();
    await expect(page).toHaveURL(/\/chat\?draft=/);
  });

  test("настройки: правила агента отдельно от заметок, правило статуса удаляется через подтверждение", async ({
    page,
  }) => {
    await open(page, "/study?view=sources", "connected");
    const memory = page.getByRole("region", { name: "Что помнит агент" });
    await expect(
      memory.getByRole("region", { name: "Правила для агента" }).getByRole("listitem"),
    ).toHaveCount(2);
    await expect(memory.getByRole("region", { name: "Заметки" }).getByRole("listitem")).toHaveCount(2);
    await memory.getByRole("button", { name: "Удалить правило статуса «Решение прикреплено»" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Удалить" }).click();
    await expect(memory.getByRole("region", { name: "Правила статусов" })).toHaveCount(0);
  });

  test("старые записи без слоёв (форма production): карточки курсов, подпись не шкала, без горизонтальной прокрутки", async ({
    page,
  }) => {
    await open(page, "/study?data=legacy", "connected");
    const obzh = card(page, "Безопасность жизнедеятельности \\(2025\\/2026 модули: 1,2\\)");
    await expect(obzh).toBeVisible();
    await expect(obzh.locator("[data-headline-meta]")).toHaveText("Результат · оценка LMS · ещё 1");
    await expect(obzh).not.toContainText("Проверено");
    await expect(card(page, "Без предмета")).toContainText("Проверено: 5 работ");
    // Подпись «оценка LMS» не переносится по словам: одна строка.
    const meta = await obzh.locator("[data-headline-meta]").boundingBox();
    expect(meta?.height ?? 99).toBeLessThan(24);
    // Полоса работ «Без предмета» не выходит за карточку.
    const box = await card(page, "Без предмета").boundingBox();
    const strip = await card(page, "Без предмета").locator("[data-score-strip]").boundingBox();
    expect((strip?.x ?? 0) + (strip?.width ?? 0)).toBeLessThanOrEqual((box?.x ?? 0) + (box?.width ?? 0));
    await expectFitsWidth(page, page.viewportSize()?.width ?? 0, "успеваемость legacy");
    if (!wide(page)) {
      for (const width of [390, 320]) {
        await page.setViewportSize({ width, height: 800 });
        await expectFitsWidth(page, width, `успеваемость legacy ${width}`);
      }
      // Телефон: панели свёрнуты, настройки доступны.
      await expect(page.getByRole("button", { name: /Показать панели/ })).toHaveAttribute(
        "aria-expanded",
        "false",
      );
      await expect(page.getByRole("button", { name: "Настроить" })).toBeVisible();
    } else {
      await expect(page.getByRole("button", { name: /Показать панели/ })).toHaveCount(0);
    }
  });
});

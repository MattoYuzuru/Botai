import { expect, type Page, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const state = (page: Page, screen: string) => page.locator(`[data-screen=${screen}]`);

test.describe("«Учёба»: разделы", () => {
  test("у каждого вуза свои разделы в фиксированном порядке; настройки внизу", async ({ page }) => {
    await open(page, "/study", "connected");
    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    const sections = hse.getByRole("list", { name: /^Разделы/ }).getByRole("link");
    await expect(sections).toHaveCount(6);
    expect(await sections.locator("span.type-subheading").allTextContents()).toEqual([
      "Предметы",
      "Оценки",
      "Преподаватели",
      "Аудитории",
      "Новости",
      "Документы и вложения",
    ]);
    const cu = page.getByRole("region", { name: /^Центральный университет/ });
    await expect(cu.getByRole("list", { name: /^Разделы/ }).getByRole("link")).toHaveCount(4);
    await expect(cu.getByRole("link", { name: /Аудитории|Документы/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Выйти из BotAI" })).toBeVisible();
  });

  test("вуз — единица со многими источниками: название, сокращение, источники со статусом и сайтом", async ({
    page,
  }) => {
    await open(page, "/study", "connected");
    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    await expect(hse.getByText("ВШЭ", { exact: true })).toBeVisible();
    const sources = hse.getByRole("list", { name: "Источники: ВШЭ" });
    await expect(sources.getByRole("listitem")).toHaveCount(2);
    await expect(sources.getByRole("listitem").first()).toContainText("Личный кабинет");
    await expect(sources.getByRole("listitem").first()).toContainText("Работает");
    await expect(sources.getByRole("listitem").nth(1)).toContainText("Расписание (календарь)");
    const site = sources.getByRole("link", { name: "Открыть сайт: Личный кабинет" });
    await expect(site).toHaveAttribute("href", "https://lk.fakeuni.test/");
    await expect(site).toHaveAttribute("target", "_blank");
    // Хост не подменяет название источника.
    await expect(sources).not.toContainText("fakeuni.test");
  });

  test("«Добавить источник» и «Подключить ещё вуз» ведут в чат с готовой просьбой; /connect нет", async ({
    page,
  }) => {
    await open(page, "/study", "connected");
    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    await expect(page.locator('a[href^="/connect"]')).toHaveCount(0);
    await hse.getByRole("link", { name: "Добавить источник" }).click();
    await expect(page).toHaveURL(/\/chat\?draft=/);
    expect(new URL(page.url()).searchParams.get("draft")).toBe(
      "Давай добавим для Высшая школа экономики ещё один источник: ",
    );
    await open(page, "/study", "connected");
    await page.getByRole("link", { name: "Подключить ещё вуз" }).click();
    await expect(page).toHaveURL(/\/chat\?draft=/);
    expect(new URL(page.url()).searchParams.get("draft")).toBe("Давай подключим вуз ");
  });

  test("раздел открывается из панели, «Назад» возвращает на «Учёбу»", async ({ page }) => {
    await open(page, "/study", "connected");
    await page
      .getByRole("region", { name: /^Высшая школа экономики/ })
      .getByRole("list", { name: /^Разделы/ })
      .getByRole("link", { name: /Оценки/ })
      .click();
    await expect(page).toHaveURL(/\/study\/grades\?space=/);
    await expect(page.getByRole("heading", { level: 1, name: "Оценки" })).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Математический анализ" }).getByText("8", { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Назад", exact: true }).click();
    await expect(page).toHaveURL(/\/study(\?[^/]*)?$/);
  });

  test("системный жест «назад» работает на экране раздела", async ({ page }) => {
    await open(page, "/study", "connected");
    await page
      .getByRole("link", { name: /Предметы/ })
      .first()
      .click();
    await expect(page.getByRole("heading", { level: 1, name: "Предметы" })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("heading", { level: 1, name: "Учёба" })).toBeVisible();
  });

  test("оценки плитками: на 390 px в одну колонку, на 430 px и шире — в две", async ({ page }) => {
    await open(page, "/study/grades", "connected");
    const tiles = page.getByRole("region", { name: "Алгоритмы и структуры данных" }).getByRole("listitem");
    await expect(tiles).toHaveCount(2);
    for (const width of [390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      const tops = await tiles.evaluateAll((nodes) =>
        nodes.map((node) => Math.round(node.getBoundingClientRect().top)),
      );
      if (width === 390) expect(tops[1], "одна колонка").toBeGreaterThan(tops[0] ?? 0);
      else expect(tops[1], "две колонки").toBe(tops[0]);
    }
  });

  test("карточки записей: предмет, преподаватель, новость, документ, аудитория", async ({ page }) => {
    await open(page, "/study/courses", "connected");
    await page.getByRole("button", { name: /Data Science/ }).click();
    await expect(page.getByRole("dialog", { name: "Data Science" }).getByText(/Получено/)).toBeVisible();
    await page.keyboard.press("Escape");

    await open(page, "/study/news", "connected");
    await page.getByRole("button", { name: /Изменения в расписании/ }).click();
    await expect(page.getByRole("dialog").getByText(/аудиторию 309/)).toBeVisible();
    await page.keyboard.press("Escape");

    await open(page, "/study/locations", "connected");
    await expect(page.getByText(/Собрано из расписания на ближайшие четыре недели/)).toBeVisible();
    await page.getByRole("button", { name: /ауд\. 305/ }).click();
    await expect(page.getByRole("dialog", { name: "ауд. 305" })).toBeVisible();
  });

  test("подключения: статус каждого источника человеческим языком", async ({ page }) => {
    await open(page, "/study/connections", "reauth");
    const lms = page.locator("li[data-connection-status=needs_reauth]");
    await expect(lms).toContainText("Нужно снова войти");
    await expect(page.locator("li[data-connection-status=active]").first()).toContainText("Работает");
    await expect(page.getByText(/needs_reauth|source_auth_required/)).toHaveCount(0);
  });

  test("нет данных: «Предметов пока нет», состояние empty", async ({ page }) => {
    await open(page, "/study/courses?data=quiet", "connected");
    await expect(page.getByText("Предметов пока нет")).toBeVisible();
    await expect(state(page, "study-courses")).toHaveAttribute("data-state", "empty");
  });

  test("нужен вход: на «Учёбе» полоса «Войти снова» и статус у источника", async ({ page }) => {
    await open(page, "/study", "reauth");
    await expect(page.getByRole("link", { name: "Войти снова" })).toBeVisible();
    await expect(
      page
        .getByRole("region", { name: /^Центральный университет/ })
        .getByRole("list", { name: "Источники: ЦУ" })
        .getByText("Нужно снова войти", { exact: true }),
    ).toBeVisible();
    await expect(state(page, "study")).toHaveAttribute("data-state", "reauth");
  });

  test("нет сети: раздел показывает последний снимок", async ({ page, context }) => {
    await open(page, "/study/news", "connected");
    await expect(page.getByRole("button", { name: /Изменения в расписании/ })).toBeVisible();
    await context.setOffline(true);
    await expect(page.getByText(/Нет сети\. Показан снимок от/).first()).toBeVisible();
    await expect(state(page, "study-news")).toHaveAttribute("data-state", "offline");
    await expect(page.getByRole("button", { name: /Изменения в расписании/ })).toBeVisible();
  });

  test("зоны нажатия, кегль и отсутствие горизонтальной прокрутки на 320/390/430", async ({ page }) => {
    for (const path of ["/study", "/study/courses", "/study/grades", "/study/people", "/study/connections"]) {
      await open(page, path, "connected");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator("[aria-busy=true]")).toHaveCount(0);
      expect(await findSmallTapTargets(page), `зоны ${path}`).toEqual([]);
      expect(await findTinyText(page), `кегль ${path}`).toEqual([]);
    }
    if ((page.viewportSize()?.width ?? 0) >= 1024) return;
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      for (const path of ["/study", "/study/grades", "/study/connections"]) {
        await open(page, path, "connected");
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.locator("[aria-busy=true]")).toHaveCount(0);
        await expectFitsWidth(page, width, `${path} ${width}`);
      }
    }
  });
});

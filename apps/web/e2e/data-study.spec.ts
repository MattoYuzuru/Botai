import { expect, type Page, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const state = (page: Page, screen: string) => page.locator(`[data-screen=${screen}]`);

test.describe("«Учёба»: разделы", () => {
  test("у каждого вуза свои разделы в фиксированном порядке; настройки внизу", async ({ page }) => {
    await open(page, "/study", "connected");
    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    const sections = hse.getByRole("list", { name: /^Разделы/ }).getByRole("link");
    await expect(sections).toHaveCount(5);
    expect(await sections.locator("span.type-subheading").allTextContents()).toEqual([
      "Предметы",
      "Оценки",
      "Преподаватели",
      "Аудитории",
      "Новости",
    ]);
    const cu = page.getByRole("region", { name: /^Центральный университет/ });
    await expect(cu.getByRole("list", { name: /^Разделы/ }).getByRole("link")).toHaveCount(4);
    await expect(cu.getByRole("link", { name: /Аудитории|Документы/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Выйти из BotAI" })).toBeVisible();
  });

  test("«Добавить источник» ведёт в чат с готовой просьбой; отдельных мастеров нет", async ({ page }) => {
    await open(page, "/study", "connected");
    const hse = page.getByRole("region", { name: /^Высшая школа экономики/ });
    await expect(page.locator('a[href^="/connect"]')).toHaveCount(0);
    await expect(page.locator('a[href^="/study/connections"]')).toHaveCount(0);
    await hse.getByRole("link", { name: "Добавить источник" }).click();
    await expect(page).toHaveURL(/\/chat\?draft=/);
    expect(new URL(page.url()).searchParams.get("draft")).toContain("Высшая школа экономики");
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

  test("карточки записей: предмет, преподаватель, новость, аудитория", async ({ page }) => {
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

  test("нет данных: «Предметов пока нет», состояние empty", async ({ page }) => {
    await open(page, "/study/courses?data=quiet", "connected");
    await expect(page.getByText("Предметов пока нет")).toBeVisible();
    await expect(state(page, "study-courses")).toHaveAttribute("data-state", "empty");
  });

  test("нужен вход: у источника статус словами, действие ведёт в чат", async ({ page }) => {
    await open(page, "/study", "reauth");
    const cu = page.getByRole("region", { name: /^Центральный университет/ });
    const sources = cu.getByRole("list", { name: "Источники: ЦУ" });
    await expect(sources.getByText("Нужен вход", { exact: true })).toBeVisible();
    await expect(cu.getByRole("link", { name: "Войти снова: LMS" })).toBeVisible();
    await expect(state(page, "study")).toHaveAttribute("data-state", "reauth");
    await expect(page.getByText(/needs_reauth|source_auth_required/)).toHaveCount(0);
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
    for (const path of ["/study", "/study/courses", "/study/grades", "/study/people"]) {
      await open(page, path, "connected");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator("[aria-busy=true]")).toHaveCount(0);
      expect(await findSmallTapTargets(page), `зоны ${path}`).toEqual([]);
      expect(await findTinyText(page), `кегль ${path}`).toEqual([]);
    }
    if ((page.viewportSize()?.width ?? 0) >= 1024) return;
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      for (const path of ["/study", "/study/grades"]) {
        await open(page, path, "connected");
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.locator("[aria-busy=true]")).toHaveCount(0);
        await expectFitsWidth(page, width, `${path} ${width}`);
      }
    }
  });
});

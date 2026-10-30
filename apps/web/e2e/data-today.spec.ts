import { expect, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.describe("«Сегодня»", () => {
  test.beforeEach(async ({ page }) => freezeClock(page));

  test("пары из обоих вузов одной лентой с метками вуза, пересечение и «Изменено» видны", async ({
    page,
  }) => {
    await open(page, "/", "connected");
    const classes = page.getByRole("region", { name: "Пары", exact: true });
    await expect(classes.getByRole("listitem")).toHaveCount(4);
    await expect(classes.getByText("Пересекается по времени")).toHaveCount(2);
    await expect(classes.getByText("Изменено")).toHaveCount(1);
    await expect(classes.getByText("ВШЭ").first()).toBeVisible();
    await expect(classes.getByText("ЦУ")).toBeVisible();
    // Статусы вузов — чипами в шапке: у каждого вуза свой, пояснение раскрывается касанием.
    const bar = page.locator("header").getByRole("list", { name: "Статусы вузов" });
    await expect(bar.getByRole("listitem")).toHaveCount(2);
    await page.getByRole("button", { name: /^ВШЭ: Обновлено/ }).click();
    await expect(bar.getByText(/Обновлено\s+\d+\s+мин/).first()).toBeVisible();
  });

  test("три ближайших срока, «Просрочено» без укоров; «Все задания» ведут на вкладку", async ({ page }) => {
    await open(page, "/", "connected");
    const deadlines = page.getByRole("region", { name: "Ближайшие сроки" });
    await expect(deadlines.getByRole("listitem")).toHaveCount(3);
    await expect(deadlines.getByText("Просрочено")).toBeVisible();
    await expect(deadlines.getByText(/Осталось\s+\d+/)).toBeVisible();
    await deadlines.getByRole("link", { name: /Все задания/ }).click();
    await expect(page).toHaveURL(/\/tasks$/);
  });

  test("карточка пары открывается шитом: аудитория, пересечение с парой другого вуза, источник", async ({
    page,
  }) => {
    await open(page, "/", "connected");
    await page
      .getByRole("region", { name: "Пары", exact: true })
      .getByRole("button", { name: /Алгоритмы и структуры данных/ })
      .click();
    const sheet = page.getByRole("dialog", { name: "Алгоритмы и структуры данных" });
    await expect(sheet.getByText("ауд. 412")).toBeVisible();
    await expect(sheet.getByText(/Основы продуктового менеджмента.*ЦУ/)).toBeVisible();
    await expect(sheet.getByText(/Получено/)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
  });

  test("до первого вуза — приглашение подключить; «всё сдано» не показывается", async ({ page }) => {
    await open(page, "/", "empty");
    await expect(page.getByText("Вузов пока нет")).toBeVisible();
    await expect(page.getByText(/сдано/i)).toHaveCount(0);
  });

  test("вузы подключены, данных нет: «Сегодня пар нет» и «Ближайших сроков нет», состояние empty", async ({
    page,
  }) => {
    await open(page, "/?data=quiet", "connected");
    await expect(page.getByText("Сегодня пар нет")).toBeVisible();
    await expect(page.getByText("Ближайших сроков нет")).toBeVisible();
    await expect(page.locator("[data-screen=today]")).toHaveAttribute("data-state", "empty");
  });

  test("пустые состояния — компактные строки, прижатые к верху колонок: огромных пустых зон нет", async ({
    page,
  }) => {
    await open(page, "/?data=quiet", "connected");
    const classes = page.getByRole("region", { name: "Пары", exact: true });
    const deadlines = page.getByRole("region", { name: "Ближайшие сроки" });
    await expect(classes.getByText("Сегодня пар нет")).toBeVisible();
    await expect(deadlines.getByText("Ближайших сроков нет")).toBeVisible();
    // Поиск ближайших пар — отдельный запрос: до его завершения между разделами стоит скелетон.
    await expect(classes.getByText("В расписании на ближайшие три недели пар тоже нет.")).toBeVisible();
    const a = await classes.boundingBox();
    const b = await deadlines.boundingBox();
    expect(a && b).toBeTruthy();
    if (!a || !b) return;
    // Каждый раздел — заголовок и одна строка, а не блок на всю высоту экрана.
    expect(a.height).toBeLessThan(160);
    expect(b.height).toBeLessThan(200);
    const width = page.viewportSize()?.width ?? 0;
    if (width >= 1024) {
      // Две колонки начинаются на одной высоте: пустая колонка не уезжает вниз по центру.
      expect(Math.abs(b.y - a.y)).toBeLessThan(2);
      expect(b.x).toBeGreaterThan(a.x + a.width - 1);
    } else {
      // На телефоне одна колонка: сроки сразу под парами.
      expect(b.y).toBeGreaterThan(a.y + a.height - 1);
      expect(b.y - (a.y + a.height)).toBeLessThan(64);
    }
  });

  test("сегодня пар нет: «Сегодня пар нет» и ниже «Ближайшие пары» — день недели, дата, компактные карточки", async ({
    page,
  }) => {
    await open(page, "/?data=freeday", "connected");
    const today = page.getByRole("region", { name: "Пары", exact: true });
    await expect(today.getByText("Сегодня пар нет")).toBeVisible();
    await expect(today.getByRole("listitem")).toHaveCount(0);
    const upcoming = page.getByRole("region", { name: "Ближайшие пары" });
    await expect(upcoming.getByRole("heading", { level: 3, name: "среда, 28 октября" })).toBeVisible();
    await expect(upcoming.getByRole("listitem")).toHaveCount(3);
    // Карточки того же вида, что в ленте дня: тип занятия, аудитория, преподаватель.
    const first = upcoming.getByRole("listitem").first();
    await expect(first).toContainText("Дискретная математика");
    await expect(first).toContainText("Лекция");
    await expect(first).toContainText("ауд. 218");
    const a = await today.boundingBox();
    const b = await upcoming.boundingBox();
    expect(a && b).toBeTruthy();
    if (a && b) {
      // «Ближайшие пары» идут сразу под строкой «Сегодня пар нет»: между ними нет пустой зоны.
      expect(b.y).toBeGreaterThan(a.y + a.height - 1);
      expect(b.y - (a.y + a.height)).toBeLessThan(48);
    }
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
    await first.getByRole("button").click();
    await expect(
      page.getByRole("dialog", { name: "Дискретная математика" }).getByText("ауд. 218"),
    ).toBeVisible();
    await expectFitsWidth(page, page.viewportSize()?.width ?? 0, "«Сегодня» без пар");
  });

  test("нужен повторный вход: полоса с «Войти снова», данные на месте и помечены устаревшими", async ({
    page,
  }) => {
    await open(page, "/", "reauth");
    const notices = page.getByRole("list", { name: "Состояние источников" });
    await expect(notices.getByText(/Нужно снова войти в личный кабинет ЦУ/)).toBeVisible();
    const action = notices.getByRole("link", { name: "Войти снова" });
    await expect(action).toBeVisible();
    // Источнику ЦУ нужно войти снова: плашка ведёт в чат с готовой просьбой, а не на старую страницу подключения.
    await expect(action).toHaveAttribute("href", /^\/chat\?draft=/);
    await expect(page.locator("[data-screen=today]")).toHaveAttribute("data-state", "reauth");
    await expect(page.getByRole("region", { name: "Пары", exact: true }).getByRole("listitem")).toHaveCount(
      4,
    );
  });

  test("вуз что-то поменял: «Варя чинит», кнопок повтора нет нигде", async ({ page }) => {
    await open(page, "/?data=repairing", "connected");
    await expect(
      page.getByRole("list", { name: "Состояние источников" }).getByText("Вуз что-то поменял, Варя чинит"),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toHaveCount(0);
    await expect(page.locator("[data-screen=today]")).toHaveAttribute("data-state", "repairing");
  });

  test("нет сети: последний снимок остаётся, подпись «Нет сети. Показан снимок от …»", async ({
    page,
    context,
  }) => {
    await open(page, "/", "connected");
    await expect(page.getByRole("region", { name: "Пары", exact: true }).getByRole("listitem")).toHaveCount(
      4,
    );
    await context.setOffline(true);
    await page.getByRole("button", { name: /^ВШЭ: Нет сети/ }).click();
    await expect(page.getByText(/Нет сети\. Показан снимок от/).first()).toBeVisible();
    await expect(page.locator("[data-screen=today]")).toHaveAttribute("data-state", "offline");
    await expect(page.getByRole("region", { name: "Пары", exact: true }).getByRole("listitem")).toHaveCount(
      4,
    );
    await context.setOffline(false);
    await expect(page.locator("[data-screen=today]")).toHaveAttribute("data-state", "ok");
  });

  test("раскладка: на ноутбуке лента дня и сроки рядом, на телефоне — друг под другом", async ({ page }) => {
    await open(page, "/", "connected");
    const classes = page.getByRole("region", { name: "Пары", exact: true });
    const deadlines = page.getByRole("region", { name: "Ближайшие сроки" });
    await expect(deadlines.getByRole("listitem")).toHaveCount(3);
    const a = await classes.boundingBox();
    const b = await deadlines.boundingBox();
    const width = page.viewportSize()?.width ?? 0;
    expect(a && b).toBeTruthy();
    if (!a || !b) return;
    if (width >= 1024) expect(b.x).toBeGreaterThan(a.x + a.width - 1);
    else expect(b.y).toBeGreaterThan(a.y + a.height - 1);
  });

  test("зоны нажатия не меньше 44 px, текст не мельче 13 px", async ({ page }) => {
    await open(page, "/", "connected");
    await expect(page.getByRole("region", { name: "Пары", exact: true }).getByRole("listitem")).toHaveCount(
      4,
    );
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
  });

  test("без горизонтальной прокрутки на 320, 390 и 430 px", async ({ page }) => {
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await open(page, "/", "connected");
      await expect(page.getByRole("region", { name: "Пары", exact: true }).getByRole("listitem")).toHaveCount(
        4,
      );
      await expectFitsWidth(page, width, `«Сегодня» ${width}`);
    }
  });
});

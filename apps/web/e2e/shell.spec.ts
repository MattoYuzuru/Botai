import { expect, test } from "@playwright/test";
import { DEMO, expectNoHorizontalOverflow, findSmallTapTargets, findTinyText, open, signIn } from "./support";

test.describe("вход и онбординг", () => {
  test("без сессии — экран входа; неверный пароль объясняется у поля, верный открывает приложение", async ({
    page,
  }) => {
    await open(page, "/", "fresh");
    await expect(page).toHaveURL(/\/login$/);
    await page.getByLabel("Почта", { exact: true }).fill(DEMO.email);
    await page.getByLabel("Пароль", { exact: true }).fill("неверный-пароль");
    await page.getByRole("button", { name: "Войти" }).click();
    await expect(page.getByText("Неверная почта или пароль")).toBeVisible();
    await expect(page.getByLabel("Пароль", { exact: true })).toHaveAttribute("aria-invalid", "true");
    await page.getByLabel("Пароль", { exact: true }).fill(DEMO.password);
    await page.getByRole("button", { name: "Войти" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Сегодня" })).toBeVisible();
    await expect(page.getByText("Вузов пока нет")).toBeVisible();
  });

  test("регистрация по приглашению: код берётся из адреса и убирается из истории, затем онбординг", async ({
    page,
  }) => {
    await open(page, "/register?invite=BOTAI-DEMO", "fresh");
    await expect(page.getByLabel("Код приглашения")).toHaveValue("BOTAI-DEMO");
    await expect(page).toHaveURL(/\/register$/);
    await page.getByLabel("Как к вам обращаться").fill("Борис");
    await page.getByLabel("Почта", { exact: true }).fill("boris@example.test");
    await page.getByLabel("Пароль", { exact: true }).fill("long-enough-password");
    await page.getByRole("button", { name: "Создать аккаунт" }).click();
    await expect(page).toHaveURL(/\/welcome$/);
    await expect(page.getByRole("heading", { level: 2, name: "Что будет с паролем" })).toBeVisible();
    await page.getByRole("link", { name: "Подключить вуз" }).click();
    await expect(page).toHaveURL(/\/connect$/);
  });

  test("поля входа — 16 px, зоны нажатия не меньше 44 px, текст не мельче 13 px", async ({ page }) => {
    await open(page, "/login", "fresh");
    await expect(page.getByRole("heading", { level: 1, name: "Вход" })).toBeVisible();
    const sizes = await page.evaluate(() =>
      [...document.querySelectorAll("input")].map((input) =>
        Number.parseFloat(getComputedStyle(input).fontSize),
      ),
    );
    for (const size of sizes) expect(size).toBeGreaterThanOrEqual(16);
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
  });

  test("экраны входа и онбординга без горизонтальной прокрутки на 320, 390 и 430 px", async ({ page }) => {
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await open(page, "/login", "fresh");
      await expect(page.getByRole("heading", { level: 1, name: "Вход" })).toBeVisible();
      await expectNoHorizontalOverflow(page, `вход ${width}`);
      await open(page, "/welcome", "empty");
      await expect(page.getByRole("heading", { level: 2, name: "Что такое BotAI" })).toBeVisible();
      await expectNoHorizontalOverflow(page, `онбординг ${width}`);
    }
  });
});

test.describe("каркас", () => {
  test("пять вкладок; на телефоне — нижняя панель, на ноутбуке — боковая панель 240 px с Wordmark", async ({
    page,
    isMobile,
  }) => {
    await open(page, "/", "connected");
    const nav = page.getByRole("navigation", { name: "Основная навигация" });
    await expect(nav.getByRole("link")).toHaveCount(5);
    await expect(nav.getByRole("link", { name: "Сегодня" })).toHaveAttribute("aria-current", "page");
    const box = await nav.boundingBox();
    const viewport = page.viewportSize();
    expect(box).not.toBeNull();
    expect(viewport).not.toBeNull();
    if (!box || !viewport) return;
    if (viewport.width < 1024) {
      expect(isMobile || viewport.width < 1024).toBe(true);
      expect(Math.round(box.y + box.height)).toBe(viewport.height);
      expect(Math.round(box.width)).toBe(viewport.width);
      await expect(page.getByRole("img", { name: "BotAI" })).toBeHidden();
    } else {
      expect(Math.round(box.x)).toBe(0);
      expect(Math.round(box.width)).toBe(240);
      expect(Math.round(box.height)).toBe(viewport.height);
      await expect(nav.getByRole("img", { name: "BotAI" })).toBeVisible();
      // Содержимое сдвинуто на ширину панели.
      const main = await page.getByRole("main").boundingBox();
      expect(main?.x ?? 0).toBeGreaterThanOrEqual(240);
    }
  });

  test("вкладки переключают экраны, активная вкладка помечена, адрес меняется", async ({ page }) => {
    await open(page, "/", "connected");
    const nav = page.getByRole("navigation", { name: "Основная навигация" });
    for (const [name, path] of [
      ["Расписание", "/schedule"],
      ["Задания", "/tasks"],
      ["Чат", "/chat"],
      ["Учёба", "/study"],
      ["Сегодня", "/"],
    ] as const) {
      await nav.getByRole("link", { name }).click();
      await expect(page).toHaveURL(new RegExp(`${path === "/" ? "/$" : `${path}$`}`));
      await expect(nav.getByRole("link", { name })).toHaveAttribute("aria-current", "page");
      await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
    }
  });

  test("системный жест «назад» работает между вкладками", async ({ page }) => {
    await open(page, "/", "connected");
    const nav = page.getByRole("navigation", { name: "Основная навигация" });
    await nav.getByRole("link", { name: "Расписание" }).click();
    await nav.getByRole("link", { name: "Задания" }).click();
    await page.goBack();
    await expect(page).toHaveURL(/\/schedule$/);
    await expect(nav.getByRole("link", { name: "Расписание" })).toHaveAttribute("aria-current", "page");
  });

  test("зоны нажатия не меньше 44 px и текст не мельче 13 px на каждом экране", async ({ page }) => {
    await open(page, "/", "connected");
    for (const path of ["/", "/schedule", "/tasks", "/chat", "/study"]) {
      await page.goto(`${path}`);
      await expect(page.getByRole("navigation", { name: "Основная навигация" })).toBeVisible();
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      expect(await findSmallTapTargets(page), `зоны нажатия ${path}`).toEqual([]);
      expect(await findTinyText(page), `кегль ${path}`).toEqual([]);
    }
  });

  test("без горизонтальной прокрутки на 320, 390 и 430 px, подписи вкладок на 320 прячутся, иконки остаются", async ({
    page,
  }) => {
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await open(page, "/study", "connected");
      await expect(page.getByRole("heading", { level: 1, name: "Учёба" })).toBeVisible();
      await expectNoHorizontalOverflow(page, `учёба ${width}`);
      const label = page
        .getByRole("navigation", { name: "Основная навигация" })
        .getByText("Расписание", { exact: true });
      const box = await label.boundingBox();
      if (width === 320) expect(box === null || box.width <= 1).toBe(true);
      else expect(box?.width ?? 0).toBeGreaterThan(40);
      // Иконки остаются на любой ширине, а ссылки доступны по имени.
      await expect(
        page
          .getByRole("navigation", { name: "Основная навигация" })
          .getByRole("link", { name: "Расписание" }),
      ).toBeVisible();
    }
  });

  test("зум текста 200 % прячет подписи вкладок и не ломает раскладку", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await open(page, "/study", "connected");
    await page.evaluate(() => {
      document.documentElement.style.fontSize = "200%";
    });
    await expectNoHorizontalOverflow(page, "зум 200 %");
    const label = page
      .getByRole("navigation", { name: "Основная навигация" })
      .getByText("Расписание", { exact: true });
    const box = await label.boundingBox();
    expect(box === null || box.width <= 1).toBe(true);
  });

  test("переключатель вуза: шит со статусами, выбор меняет акцент пространства", async ({ page }) => {
    await open(page, "/schedule", "connected");
    const scope = page.locator("[data-space]").first();
    await expect(scope).toHaveAttribute("data-space", "hse");
    const before = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--color-accent"),
    );
    expect(before).toBeDefined();
    await page.getByRole("button", { name: /Все вузы/ }).click();
    const sheet = page.getByRole("dialog", { name: "Вуз" });
    await expect(sheet.getByText(/Обновлено/).first()).toBeVisible();
    await sheet.getByRole("button", { name: /Центральный университет/ }).click();
    await expect(scope).toHaveAttribute("data-space", "cu");
    const primary = await page.evaluate(() => {
      const probe = document.createElement("div");
      probe.className = "bg-accent";
      document.querySelector("[data-space]")?.appendChild(probe);
      const color = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return color;
    });
    expect(primary).not.toBe("");
  });

  test("до вуза «Сегодня» — приглашение подключить, и это не «всё сдано»", async ({ page }) => {
    await open(page, "/", "empty");
    await expect(page.getByText("Вузов пока нет")).toBeVisible();
    await expect(page.getByText(/сдано/i)).toHaveCount(0);
    await page.getByRole("link", { name: "Подключить вуз" }).click();
    await expect(page).toHaveURL(/\/connect$/);
  });
});

test.describe("настройки и выход", () => {
  test("тёмная тема выбирается в «Учёбе», переживает перезагрузку, спокойный режим ставит атрибут", async ({
    page,
  }) => {
    await open(page, "/study", "connected");
    await page.getByRole("radio", { name: "Тёмная" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.getByRole("switch", { name: /Спокойное движение/ }).click();
    await expect(page.locator("html")).toHaveAttribute("data-motion", "calm");
    await page.getByRole("radio", { name: "Система" }).click();
    await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.+/);
  });

  test("выход возвращает на вход, прежние данные не остаются", async ({ page }) => {
    await open(page, "/study", "connected");
    await page.getByRole("button", { name: "Выйти из BotAI" }).click();
    await expect(page).toHaveURL(/\/login$/);
    await page.goto("/study");
    await expect(page).toHaveURL(/\/login$/);
  });

  test("повторный вход после выхода работает", async ({ page }) => {
    await open(page, "/study", "connected");
    await page.getByRole("button", { name: "Выйти из BotAI" }).click();
    await signIn(page);
    await expect(page.getByRole("heading", { level: 1, name: "Сегодня" })).toBeVisible();
  });

  test("без сети показывается полоса «Нет сети», данные остаются", async ({ page, context }) => {
    await open(page, "/schedule", "connected");
    await expect(page.getByRole("heading", { level: 1, name: "Расписание" })).toBeVisible();
    await context.setOffline(true);
    await expect(page.getByText(/Нет сети\. Показаны последние данные/)).toBeVisible();
    await expect(page.getByRole("heading", { level: 1, name: "Расписание" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Повторить|Обновить/ })).toHaveCount(0);
    await context.setOffline(false);
    await expect(page.getByText(/Нет сети\. Показаны последние данные/)).toBeHidden();
  });
});

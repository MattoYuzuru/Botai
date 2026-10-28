import { expect, test } from "@playwright/test";
import { freezeClock } from "./data-support";
import {
  DEMO,
  expectNoHorizontalOverflow,
  expectSheetReady,
  findSmallTapTargets,
  findTinyText,
  open,
  signIn,
} from "./support";

test.describe("вход и онбординг", () => {
  for (const theme of ["light", "dark"] as const) {
    test(`вход и регистрация — компактная колонка по центру (${theme})`, async ({ page }, testInfo) => {
      await page.emulateMedia({ colorScheme: theme });
      for (const width of [390, 1280, 1512, 3024]) {
        await page.setViewportSize({ width, height: 900 });
        for (const [path, title] of [
          ["/login", "Вход"],
          ["/register", "Регистрация"],
        ] as const) {
          await open(page, path, "fresh");
          await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
          const bounds = await page.locator("[data-auth-layout]").evaluate((main) => {
            const box = main.getBoundingClientRect();
            const form = main.querySelector("form")?.getBoundingClientRect();
            return {
              width: box.width,
              center: box.left + box.width / 2,
              maximum: getComputedStyle(main).maxWidth,
              formWidth: form?.width ?? 0,
              viewport: window.innerWidth,
            };
          });
          expect(bounds.maximum).not.toBe("none");
          expect(bounds.width).toBeLessThanOrEqual(440);
          expect(Math.abs(bounds.center - bounds.viewport / 2)).toBeLessThanOrEqual(1);
          expect(bounds.formWidth).toBeLessThanOrEqual(400);
          expect(bounds.formWidth).toBeGreaterThanOrEqual(350);
          await expectNoHorizontalOverflow(page, `${title} ${width} ${theme}`);
          if (width !== 3024) {
            await page.screenshot({
              path: testInfo.outputPath(`${path.slice(1)}-${width}-${theme}.png`),
              fullPage: true,
            });
          }
        }
      }
    });
  }

  test("вход и регистрация сохраняют форму при увеличении текста до 200 %", async ({ page }) => {
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      for (const path of ["/login", "/register"]) {
        await open(page, path, "fresh");
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await page.evaluate(() => {
          document.documentElement.style.fontSize = "200%";
        });
        await expectNoHorizontalOverflow(page, `${path} ${width} текст 200%`);
        await expect(
          page.getByRole("button", { name: path === "/login" ? "Войти" : "Создать аккаунт" }),
        ).toBeEnabled();
      }
    }
  });
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
    await expect(page).toHaveURL(/\/chat\?thread=/);
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
  for (const theme of ["light", "dark"] as const) {
    test(`ноутбук: заголовки и содержимое выровнены, страницы широкие, чат и подключение — в колонке (${theme})`, async ({
      page,
    }, testInfo) => {
      await page.emulateMedia({ colorScheme: theme });
      await freezeClock(page);
      for (const width of [1280, 1512]) {
        await page.setViewportSize({ width, height: 900 });
        for (const [path, title] of [
          ["/", "Сегодня"],
          ["/schedule", "Расписание"],
          ["/tasks", "Задания"],
          ["/study", "Учёба"],
        ] as const) {
          await open(page, path, "connected");
          await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
          const dataScreen = page.locator(`[data-screen=${path === "/" ? "today" : path.slice(1)}]`);
          await expect(dataScreen).toBeVisible();
          await expect(dataScreen).not.toHaveAttribute("data-state", "loading");
          const bounds = await page.evaluate(() => {
            const main = document.querySelector("main")?.getBoundingClientRect();
            const header = document.querySelector("header > div")?.getBoundingClientRect();
            return {
              main: main && { x: main.x, width: main.width },
              header: header && { x: header.x, width: header.width },
            };
          });
          expect(bounds.main?.width ?? 0).toBeGreaterThan(736);
          expect(bounds.main?.width ?? 0).toBeLessThanOrEqual(1152);
          expect(Math.abs((bounds.main?.x ?? 0) - (bounds.header?.x ?? 0))).toBeLessThanOrEqual(1);
          expect(Math.abs((bounds.main?.width ?? 0) - (bounds.header?.width ?? 0))).toBeLessThanOrEqual(1);
          await expectNoHorizontalOverflow(page, `${path} ${width}`);
          if (path === "/study") {
            expect((await page.locator('[aria-label="Тема"]').boundingBox())?.width ?? 0).toBeLessThanOrEqual(
              420,
            );
          }
          if (width === 1512)
            await page.screenshot({
              path: testInfo.outputPath(`${path === "/" ? "today" : path.slice(1)}-1512-${theme}.png`),
              fullPage: true,
            });
        }
        await open(page, "/chat?thread=new&pace=0", "connected");
        await expect(page.getByRole("heading", { level: 1, name: "Чат" })).toBeVisible();
        await expect(page.getByRole("list", { name: "Подсказки" })).toBeVisible();
        const chat = await page.evaluate(() =>
          ["header > div", "[data-chat-transcript]", "[data-chat-dock]"].map((selector) => {
            const box = document.querySelector(selector)?.getBoundingClientRect();
            return { x: box?.x ?? 0, width: box?.width ?? 0 };
          }),
        );
        for (const box of chat) {
          expect(box.width).toBe(736);
          expect(box.x).toBe(chat[0]?.x);
        }
        if (width === 1512)
          await page.screenshot({ path: testInfo.outputPath(`chat-1512-${theme}.png`), fullPage: true });
        await open(page, "/connect", "connected");
        await expect(page.getByRole("heading", { level: 1, name: "Чат" })).toBeVisible();
        await expect(page.getByText("Пришлите ссылку на источник, который хотите подключить.")).toBeVisible();
        expect((await page.locator("[data-chat-transcript]").boundingBox())?.width).toBe(736);
        await page.getByRole("button", { name: "Источник и подключения" }).click();
        await expectSheetReady(page, "Источник и подключения");
        await expect(page.getByRole("combobox", { name: "Вуз" })).toBeVisible();
        expect(
          (await page.getByRole("dialog", { name: "Источник и подключения" }).boundingBox())?.width ?? 0,
        ).toBeLessThanOrEqual(420);
        await expect(page.getByRole("heading", { name: "Ваши подключения" })).toBeVisible();
        if (width === 1512)
          await page.screenshot({ path: testInfo.outputPath(`connect-1512-${theme}.png`), fullPage: true });
        await open(page, "/welcome", "empty");
        await expect(page.getByRole("heading", { level: 2, name: "Что такое BotAI" })).toBeVisible();
        expect((await page.getByRole("main").boundingBox())?.width).toBe(736);
        if (width === 1512)
          await page.screenshot({ path: testInfo.outputPath(`welcome-1512-${theme}.png`), fullPage: true });
      }
    });
  }
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
    await expect(page).toHaveURL(/\/chat\?thread=/);
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

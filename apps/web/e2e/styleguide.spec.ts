import { expect, test } from "@playwright/test";

/**
 * Витрина /styleguide в настоящем браузере: то, что jsdom проверить не может — раскладка, вычисленные
 * стили, размеры зон нажатия и вложенные темы.
 */

test.describe("витрина", () => {
  test("нет горизонтальной прокрутки на 320, 390 и 430 px", async ({ page }) => {
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await page.goto("/styleguide");
      await expect(page.getByRole("heading", { level: 1, name: "Витрина компонентов" })).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `ширина ${width}`).toBeLessThanOrEqual(0);
    }
  });

  test("кегль каждого поля ввода — не меньше 16 px (iOS не приближает страницу)", async ({ page }) => {
    await page.goto("/styleguide");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const sizes = await page.evaluate(() =>
      [...document.querySelectorAll("input:not([type=hidden]), textarea")].map((element) =>
        Number.parseFloat(getComputedStyle(element).fontSize),
      ),
    );
    expect(sizes.length).toBeGreaterThan(5);
    for (const size of sizes) expect(size).toBeGreaterThanOrEqual(16);
  });

  test("минимальный кегль интерфейса — 13 px", async ({ page }) => {
    await page.goto("/styleguide");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const tooSmall = await page.evaluate(() => {
      const found: string[] = [];
      for (const element of document.querySelectorAll<HTMLElement>("body *")) {
        if (
          !element.childNodes.length ||
          ![...element.childNodes].some((n) => n.nodeType === 3 && n.textContent?.trim())
        )
          continue;
        const size = Number.parseFloat(getComputedStyle(element).fontSize);
        if (size < 12.99) found.push(`${element.tagName}.${element.className}: ${size}`);
      }
      return found;
    });
    expect(tooSmall).toEqual([]);
  });

  test("зона нажатия кнопок и ссылок — не меньше 44×44 px (с учётом расширения через ::before)", async ({
    page,
  }) => {
    await page.goto("/styleguide");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const small = await page.evaluate(() => {
      const found: string[] = [];
      const selector = "button, a[href], [role=checkbox], [role=radio], [role=switch], [role=combobox]";
      const reaches = (element: Element, x: number, y: number) => {
        const hit = document.elementFromPoint(x, y);
        return Boolean(hit && (hit === element || element.contains(hit)));
      };
      for (const element of document.querySelectorAll<HTMLElement>(selector)) {
        // Ссылка внутри предложения — часть текста: её зона определяется строкой (WCAG 2.5.8, исключение для встроенных ссылок).
        if (getComputedStyle(element).display === "inline") continue;
        // Радио и флажок живут внутри строки-подписи не ниже 44 px: зона нажатия — вся строка.
        const target = element.closest("label") ?? element;
        target.scrollIntoView({ block: "center", inline: "center" });
        const box = target.getBoundingClientRect();
        if (box.width === 0 || box.height === 0) continue;
        const centerX = box.left + box.width / 2;
        const centerY = box.top + box.height / 2;
        const growY = Math.max(0, (44 - box.height) / 2);
        const growX = Math.max(0, (44 - box.width) / 2);
        const okY =
          growY === 0 ||
          (reaches(target, centerX, box.top - growY + 1) && reaches(target, centerX, box.bottom + growY - 1));
        const okX =
          growX === 0 ||
          (reaches(target, box.left - growX + 1, centerY) && reaches(target, box.right + growX - 1, centerY));
        if (!okX || !okY) {
          const name = (element.textContent || element.getAttribute("aria-label") || "").trim().slice(0, 30);
          found.push(`${element.tagName} «${name}» ${Math.round(box.width)}×${Math.round(box.height)}`);
        }
      }
      return found;
    });
    expect(small).toEqual([]);
  });

  test("тема переключается и переживает перезагрузку", async ({ page }) => {
    await page.goto("/styleguide");
    const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    const light = await background();
    await page.getByRole("radio", { name: "Тёмная" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const dark = await background();
    expect(dark).not.toBe(light);
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    expect(await background()).toBe(dark);
  });

  test("обе темы рядом: вложенные панели получают разные цвета фона", async ({ page }) => {
    await page.goto("/styleguide");
    await page.getByRole("switch", { name: /Светлая и тёмная рядом/ }).click();
    const colors = await page.evaluate(() => {
      const bg = (id: string) =>
        getComputedStyle(document.querySelector(`[data-testid=${id}]`) as Element).backgroundColor;
      return { light: bg("panel-light"), dark: bg("panel-dark") };
    });
    expect(colors.light).not.toBe(colors.dark);
  });

  test("акцент ЦУ меняет заливку primary-кнопки и не трогает danger", async ({ page }) => {
    await page.goto("/styleguide");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const fills = () =>
      page.evaluate(() => {
        const color = (selector: string) => {
          const element = [...document.querySelectorAll<HTMLElement>(selector)].find((e) =>
            e.textContent?.includes("Подключить вуз"),
          );
          return element ? getComputedStyle(element).backgroundColor : "";
        };
        const danger = [...document.querySelectorAll<HTMLElement>("button")].find((e) =>
          e.textContent?.includes("Отключить вуз"),
        );
        return { primary: color("button"), danger: danger ? getComputedStyle(danger).backgroundColor : "" };
      });
    const before = await fills();
    await page.getByRole("radio", { name: "ЦУ" }).click();
    const after = await fills();
    expect(after.primary).not.toBe(before.primary);
    expect(after.danger).toBe(before.danger);
  });

  test("спокойное движение останавливает циклы", async ({ page }) => {
    await page.goto("/styleguide");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const running = () =>
      page.evaluate(() => document.getAnimations().filter((a) => a.playState === "running").length);
    expect(await running()).toBeGreaterThan(0);
    await page.getByRole("switch", { name: /Спокойное движение/ }).click();
    await expect(page.locator("html")).toHaveAttribute("data-motion", "calm");
    await expect.poll(running).toBe(0);
  });

  test("системное сокращённое движение уважается без настроек приложения", async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    const page = await context.newPage();
    await page.goto("/styleguide");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(() => document.getAnimations().filter((a) => a.playState === "running").length),
      )
      .toBe(0);
    await context.close();
  });

  test("зум текста 200 % не ломает раскладку", async ({ page }) => {
    await page.goto("/styleguide");
    await page.getByRole("radio", { name: "200 %" }).click();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("шит открывается, закрывается кнопкой и Esc, фокус возвращается на кнопку", async ({ page }) => {
    await page.goto("/styleguide");
    const open = page.getByRole("button", { name: "Открыть шит" });
    await open.click();
    const sheet = page.getByRole("dialog", { name: "Подключение вуза" });
    await expect(sheet).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(open).toBeFocused();
    await open.click();
    await sheet.getByRole("button", { name: "Закрыть" }).click();
    await expect(sheet).toBeHidden();
  });

  test("цвет не единственный признак: ошибка поля — иконка и текст", async ({ page }) => {
    await page.goto("/styleguide");
    const error = page.getByText("Нужна ссылка вида https://lk.example.ru");
    await expect(error).toBeVisible();
    await expect(error.locator("xpath=..").locator("svg")).toHaveCount(1);
  });
});

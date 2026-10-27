import { expect, type Locator, type Page, test } from "@playwright/test";

/**
 * Варя в настоящем браузере (витрина /styleguide#mascot): раскладка насеста и сцены, прыжок, тихие режимы.
 * То, что jsdom проверить не может: реальные размеры, наложения и CSS-переходы.
 */

const open = async (page: Page) => {
  await page.goto("/styleguide");
  await expect(page.getByRole("heading", { level: 2, name: "Варя" })).toBeVisible();
};

const frame = (page: Page, width: "320" | "390" | "430") => page.locator(`[data-frame="${width}"]`);

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const boxOf = async (locator: Locator): Promise<Box> => {
  const box = await locator.boundingBox();
  if (!box) throw new Error("элемент не виден");
  return { left: box.x, top: box.y, right: box.x + box.width, bottom: box.y + box.height };
};

const overlaps = (a: Box, b: Box) =>
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

test.describe("Варя на насесте и на сцене", () => {
  for (const width of ["320", "390", "430"] as const) {
    test(`насест на ${width} px: на кромке справа, не перекрывает текст ввода и «Отправить»`, async ({
      page,
    }) => {
      await open(page);
      const scope = frame(page, width);
      await scope.scrollIntoViewIfNeeded();
      await scope.getByRole("button", { name: "Первое сообщение" }).click();
      // Прыжок ≤ 280 мс плюс запас на кадр.
      await page.waitForTimeout(500);
      const sprite = await boxOf(scope.locator("svg.varya"));
      const field = await boxOf(scope.getByRole("textbox", { name: "Сообщение агенту" }));
      const send = await boxOf(scope.getByRole("button", { name: "Отправить" }));
      const form = await boxOf(scope.locator("form"));

      expect(overlaps(sprite, field), "перекрывает поле ввода").toBe(false);
      expect(overlaps(sprite, send), "перекрывает «Отправить»").toBe(false);
      // Лапы стоят на верхней кромке поля (запас — пустые 5 % поля спрайта).
      expect(Math.abs(sprite.bottom - form.top)).toBeLessThanOrEqual(4);
      // Справа отступ 16 px от края поля.
      expect(Math.abs(form.right - 16 - sprite.right)).toBeLessThanOrEqual(2);
      // Размер: 36 px, на экране от 430 px (поле от 390 px) — 40 px.
      const expected = form.right - form.left >= 390 ? 40 : 36;
      expect(Math.abs(sprite.right - sprite.left - expected)).toBeLessThanOrEqual(1);
      // Одна Варя на сцену.
      await expect(scope.locator("svg.varya")).toHaveCount(1);
    });
  }

  test("пустой чат: сцена фиксированной высоты, Варя стоит на кромке поля слева направо по полу", async ({
    page,
  }) => {
    await open(page);
    const scope = frame(page, "390");
    await scope.scrollIntoViewIfNeeded();
    const sprite = await boxOf(scope.locator("svg.varya"));
    const form = await boxOf(scope.locator("form"));
    // 120 px, если поле от 358 px (экран 390 px и шире), иначе 96 px. Высота сцены не зависит от того, гуляет она или стоит.
    const expected = form.right - form.left >= 358 ? 120 : 96;
    expect(Math.abs(sprite.bottom - sprite.top - expected)).toBeLessThanOrEqual(1);
    expect(Math.abs(sprite.bottom - form.top)).toBeLessThanOrEqual(8);
  });

  test("320 px: сцена 96 px", async ({ page }) => {
    await open(page);
    const scope = frame(page, "320");
    await scope.scrollIntoViewIfNeeded();
    const sprite = await boxOf(scope.locator("svg.varya"));
    expect(Math.abs(sprite.bottom - sprite.top - 96)).toBeLessThanOrEqual(1);
    // Пол не выше сцены: Варя целиком над полем ввода.
    const form = await boxOf(scope.locator("form"));
    expect(sprite.bottom - form.top).toBeLessThanOrEqual(8);
  });

  test("прыжок со сцены на насест — один экземпляр, не мгновенно и не дольше 280 мс", async ({ page }) => {
    await open(page);
    const scope = frame(page, "390");
    await scope.scrollIntoViewIfNeeded();
    const before = await scope.locator("svg.varya").elementHandle();
    // Замеряем размер Вари по кадрам прямо в странице: так нагрузка на тест не искажает время.
    const samples = await scope.evaluate(async (element) => {
      const svg = element.querySelector("svg.varya") as SVGSVGElement;
      const button = [...element.querySelectorAll("button")].find(
        (item) => item.textContent === "Первое сообщение",
      );
      const started = performance.now();
      button?.click();
      const series: { t: number; w: number }[] = [];
      await new Promise<void>((resolve) => {
        const tick = () => {
          series.push({ t: performance.now() - started, w: svg.getBoundingClientRect().width });
          if (performance.now() - started < 700) requestAnimationFrame(tick);
          else resolve();
        };
        requestAnimationFrame(tick);
      });
      return series;
    });
    const first = samples[0];
    const last = samples[samples.length - 1];
    expect(first?.w).toBeGreaterThan(90); // стартует на сцене (96 или 120 px)
    expect(Math.round(last?.w ?? 0)).toBe(36); // приземляется на насест
    // Момент, когда размер перестал меняться: после начала перелёта и не позже 280 мс (плюс кадр и запас на нагрузку).
    const settled = samples.find((sample) => Math.abs(sample.w - 36) < 0.5);
    expect(settled?.t).toBeGreaterThan(60);
    expect(settled?.t).toBeLessThanOrEqual(280 + 120);
    const same = await scope.locator("svg.varya").elementHandle();
    expect(await page.evaluate(([a, b]) => a === b, [before, same])).toBe(true);
  });

  test("reduced motion: смена места мгновенная, анимаций у Вари нет", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await open(page);
    const scope = frame(page, "390");
    await scope.scrollIntoViewIfNeeded();
    await scope.getByRole("button", { name: "Первое сообщение" }).click();
    await page.waitForTimeout(40);
    const sprite = await boxOf(scope.locator("svg.varya"));
    expect(Math.abs(sprite.right - sprite.left - 36)).toBeLessThanOrEqual(1);
    const animated = await scope.locator("svg.varya").evaluate((svg) => {
      const running: string[] = [];
      for (const element of svg.querySelectorAll("*")) {
        const name = getComputedStyle(element).animationName;
        if (name && name !== "none") running.push(name);
      }
      return running;
    });
    expect(animated).toEqual([]);
  });

  test("клавиатура открыта: Варя статична, анимаций нет, пузыря нет", async ({ page }) => {
    await open(page);
    const scope = frame(page, "390");
    await scope.scrollIntoViewIfNeeded();
    await scope.getByRole("switch", { name: "Клавиатура открыта" }).click();
    await expect(scope.locator("svg.varya")).toHaveClass(/still/);
    await expect(scope.locator("[data-dock] .pointer-events-auto")).toHaveCount(0);
  });

  test("спокойный режим: все Вари на витрине статичны, смысл остаётся текстом", async ({ page }) => {
    await page.goto("/styleguide");
    await page.getByRole("switch", { name: /Спокойное движение/ }).click();
    const section = page.locator("#mascot");
    await section.scrollIntoViewIfNeeded();
    const live = await section.locator("svg.varya:not(.still)").count();
    expect(live).toBe(0);
    await expect(section.getByText("Осталось 5 ч").first()).toBeVisible();
    await expect(section.getByRole("button", { name: "Открыть срочную" }).first()).toBeVisible();
  });

  test("позы с предметами показывают их: ключ у reauth, облако у offline, знак у overdue", async ({
    page,
  }) => {
    await open(page);
    for (const [pose, prop] of [
      ["reauth", "key"],
      ["offline", "cloudx"],
      ["overdue", "excl"],
      ["think", "bubbles"],
      ["class", "clock"],
    ] as const) {
      const shown = await page
        .locator(`[data-pose="${pose}"] [data-prop="${prop}"]`)
        .evaluate((element) => getComputedStyle(element).display);
      expect(shown, `${pose}: ${prop}`).not.toBe("none");
    }
  });

  test("витрина Вари: нет горизонтальной прокрутки на 320, 390 и 430 px", async ({ page }) => {
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await open(page);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `ширина ${width}`).toBeLessThanOrEqual(0);
    }
  });

  test("картинка Вари декоративна: ни одного фокусируемого элемента внутри спрайта", async ({ page }) => {
    await open(page);
    const focusable = await page.evaluate(() =>
      [...document.querySelectorAll("[data-mascot], svg.varya")].flatMap((host) =>
        [...host.querySelectorAll("a, button, input, [tabindex]")].map((element) => element.tagName),
      ),
    );
    expect(focusable).toEqual([]);
    const hidden = await page.evaluate(() =>
      [...document.querySelectorAll("svg.varya")].every(
        (svg) => svg.getAttribute("aria-hidden") === "true" && svg.closest("[aria-hidden=true]") !== null,
      ),
    );
    expect(hidden).toBe(true);
  });
});

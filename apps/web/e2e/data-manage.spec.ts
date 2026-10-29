import { expect, type Page, test } from "@playwright/test";
import { expectFitsWidth, findSmallTapTargets, findTinyText, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

const SECRET = "https://my.fakeuni.test/calendar/export.ics?key=very-secret-key-123";

async function openPortalSheet(page: Page) {
  await open(page, "/study/connections", "connected");
  await page.getByRole("button", { name: /Личный кабинет/ }).click();
  return page.getByRole("dialog", { name: "Личный кабинет" });
}

test.describe("«Источники»: управление", () => {
  test("карточка подключения: статус, пароль, действия; кнопки «Обновить» нет", async ({ page }) => {
    const sheet = await openPortalSheet(page);
    await expect(sheet.getByText("Работает")).toBeVisible();
    await expect(sheet.getByText(/Сохранён зашифрованным/)).toBeVisible();
    await expect(sheet.getByRole("button", { name: "Забыть пароль" })).toBeVisible();
    await expect(sheet.getByRole("button", { name: "Удалить подключение" })).toBeVisible();
    await expect(sheet.getByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toHaveCount(0);
  });

  test("ссылка календаря: значение скрыто в поле и нигде не показывается после сохранения", async ({
    page,
  }) => {
    const sheet = await openPortalSheet(page);
    await sheet.getByRole("button", { name: /Добавить ссылку на календарь или ключ/ }).click();
    const field = sheet.getByRole("textbox", { name: "Ссылка на календарь" });
    await expect(field).toHaveAttribute("type", "password");
    expect(
      await field.evaluate((input) => Number.parseFloat(getComputedStyle(input).fontSize)),
    ).toBeGreaterThanOrEqual(16);
    await field.fill(SECRET);
    await sheet.getByRole("button", { name: "Сохранить" }).click();
    await expect(page.getByText("Ссылка на календарь сохранена")).toBeVisible();
    await expect(sheet.getByRole("textbox", { name: "Ссылка на календарь" })).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText("very-secret-key-123");
    expect(
      await page.evaluate(() => [...document.querySelectorAll("input")].map((input) => input.value).join("")),
    ).not.toContain("very-secret");
  });

  test("«Забыть пароль»: подтверждение, затем пароль не сохранён", async ({ page }) => {
    const sheet = await openPortalSheet(page);
    await sheet.getByRole("button", { name: "Забыть пароль" }).click();
    const dialog = page.getByRole("alertdialog", { name: "Забыть пароль?" });
    await expect(dialog.getByRole("button", { name: "Отмена" })).toBeFocused();
    await dialog.getByRole("button", { name: "Забыть пароль" }).click();
    await expect(page.getByText("Пароль удалён")).toBeVisible();
    await expect(sheet.getByText(/Не сохранён/)).toBeVisible();
    await expect(sheet.getByRole("button", { name: "Забыть пароль" })).toHaveCount(0);
  });

  test("удаление подключения: подтверждение, подключение исчезает из списка", async ({ page }) => {
    const sheet = await openPortalSheet(page);
    await sheet.getByRole("button", { name: "Удалить подключение" }).click();
    const dialog = page.getByRole("alertdialog", { name: /Удалить «Личный кабинет»\?/ });
    await expect(dialog.getByText(/Данные этого источника исчезнут/)).toBeVisible();
    await dialog.getByRole("button", { name: "Удалить подключение" }).click();
    await expect(page.getByText("Подключение удалено")).toBeVisible();
    await expect(page.getByRole("button", { name: /Личный кабинет/ })).toHaveCount(0);
  });

  test("«Войти снова» из карточки открывает чат с готовой просьбой", async ({ page }) => {
    await open(page, "/study/connections", "reauth");
    await page.getByRole("button", { name: /LMS/ }).click();
    await page.getByRole("dialog", { name: "LMS" }).getByRole("link", { name: "Войти снова" }).click();
    await expect(page).toHaveURL(/\/chat\?draft=/);
    expect(new URL(page.url()).searchParams.get("draft")).toBe("Войди снова в «LMS» (Центральный университет)");
  });
});

test.describe("«Добавить документ»", () => {
  test("по ссылке: стадии словами, без процентов; документ появляется в списке", async ({ page }) => {
    await open(page, "/study/documents", "connected");
    await page.getByRole("button", { name: "Добавить документ" }).click();
    const sheet = page.getByRole("dialog", { name: "Добавить документ" });
    // Несколько вузов: сначала выбираем вуз.
    await sheet.getByRole("combobox").click();
    await page.getByRole("option", { name: "Высшая школа экономики" }).click();
    await sheet.getByLabel("Ссылка").fill("https://docs.fakeuni.test/sheets/exams");
    await sheet.getByLabel(/^Название/).fill("Расписание экзаменов");
    await sheet.getByRole("button", { name: "Добавить" }).click();
    await expect(sheet.getByText("Читаю документ")).toBeVisible({ timeout: 8000 });
    expect(await page.evaluate(() => document.body.textContent)).not.toMatch(/\d\s*%/);
    await expect(sheet.getByText(/Документ добавлен/)).toBeVisible({ timeout: 12_000 });
    await sheet.getByRole("button", { name: "Готово" }).click();
    await expect(page.getByRole("button", { name: /Расписание экзаменов/ })).toBeVisible();
  });

  test("файл: настоящая загрузка multipart, документ с именем файла появляется в списке", async ({
    page,
  }) => {
    await open(page, `/study/documents`, "connected");
    await page.getByRole("button", { name: "Добавить документ" }).click();
    const sheet = page.getByRole("dialog", { name: "Добавить документ" });
    await sheet.getByRole("combobox").click();
    await page.getByRole("option", { name: "Высшая школа экономики" }).click();
    await sheet.getByRole("radio", { name: "Файл" }).click();
    await sheet.locator("input[type=file]").setInputFiles({
      name: "program.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.4 test"),
    });
    await expect(sheet.getByText("program.pdf")).toBeVisible();
    await sheet.getByRole("button", { name: "Добавить" }).click();
    await expect(sheet.getByText(/Документ добавлен/)).toBeVisible({ timeout: 12_000 });
    await sheet.getByRole("button", { name: "Готово" }).click();
    await expect(page.getByRole("button", { name: /program\.pdf/ })).toBeVisible();
  });

  test("файл не того типа отклоняется до отправки, словами у поля", async ({ page }) => {
    await open(page, `/study/documents`, "connected");
    await page.getByRole("button", { name: "Добавить документ" }).click();
    const sheet = page.getByRole("dialog", { name: "Добавить документ" });
    await sheet.getByRole("combobox").click();
    await page.getByRole("option", { name: "Высшая школа экономики" }).click();
    await sheet.getByRole("radio", { name: "Файл" }).click();
    await sheet
      .locator("input[type=file]")
      .setInputFiles({ name: "app.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ") });
    await sheet.getByRole("button", { name: "Добавить" }).click();
    await expect(sheet.getByText(/Этот тип файла не поддерживается/)).toBeVisible();
  });

  test("зоны нажатия, кегль 16 px у полей и отсутствие горизонтальной прокрутки на 320/390/430", async ({
    page,
  }) => {
    // Шит выезжает 280 мс: без анимации зоны нажатия меряются на месте, а не на полпути.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await open(page, "/study/documents", "connected");
    await page.getByRole("button", { name: "Добавить документ" }).click();
    const sheet = page.getByRole("dialog", { name: "Добавить документ" });
    await expect(sheet).toBeVisible();
    const sizes = await sheet
      .locator("input:not([type=file]), textarea")
      .evaluateAll((nodes) => nodes.map((node) => Number.parseFloat(getComputedStyle(node).fontSize)));
    expect(sizes.length).toBeGreaterThan(0);
    for (const size of sizes) expect(size).toBeGreaterThanOrEqual(16);
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
    if ((page.viewportSize()?.width ?? 0) >= 1024) return;
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await expectFitsWidth(page, width, `добавить документ ${width}`);
    }
  });
});

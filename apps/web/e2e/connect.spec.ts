import { expect, type Page, test } from "@playwright/test";
import type { JobSnapshot } from "../src/features/jobs/job-state";
import { expectNoHorizontalOverflow, findSmallTapTargets, findTinyText, open } from "./support";

/**
 * Подключение вуза в настоящем браузере: поток SSE через MSW, секретное поле, свёрнутая работа в шапке и
 * запасной экран входа. Моки отвечают мгновенно (`pace=0`), чтобы тесты шли быстро.
 */

const SECRET = "correct-horse-battery-staple";

async function startHse(page: Page, variant = "") {
  await open(page, `/connect?pace=0${variant ? `&job=${variant}` : ""}`, "empty");
  await page.getByRole("radio", { name: /Высшая школа экономики/ }).click();
  await page.getByRole("button", { name: "Начать подключение" }).click();
  await expect(page).toHaveURL(/\/connect\/01J/);
  await expect(page.getByRole("heading", { level: 1, name: "Подключаю ВШЭ" })).toBeVisible();
}

async function answerText(page: Page, value: string) {
  await page.getByLabel("Ответ", { exact: true }).fill(value);
  await page.getByRole("button", { name: "Ответить" }).click();
}

test.describe("выбор вуза", () => {
  test("экран без горизонтальной прокрутки, поля — 16 px, зоны нажатия — 44 px, текст — от 13 px", async ({
    page,
  }) => {
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await open(page, "/connect", "empty");
      await expect(page.getByRole("radio", { name: /Высшая школа экономики/ })).toBeVisible();
      await expectNoHorizontalOverflow(page, `выбор вуза ${width}`);
    }
    const sizes = await page.evaluate(() =>
      [...document.querySelectorAll("input:not([aria-hidden=true])")].map((input) =>
        Number.parseFloat(getComputedStyle(input).fontSize),
      ),
    );
    for (const size of sizes) expect(size).toBeGreaterThanOrEqual(16);
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
  });
});

test.describe("ProgressWall", () => {
  test("весь путь: логин, секретный пароль, выбор предметов, итог, «Готово»", async ({ page }) => {
    await startHse(page);
    await expect(page.getByText("Сколько осталось — пока неизвестно")).toBeVisible();
    await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
    await expect(page.getByText("Вход через поля BotAI")).toBeVisible();
    await expect(page.getByRole("button", { name: "Открыть браузер" })).toBeVisible();

    // Экран с вопросом: ничего не вылезает за край, зоны нажатия и кегль в норме.
    await expectNoHorizontalOverflow(page, "вопрос");
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);

    await answerText(page, "anna");
    await expect(page.getByText("Вход в lk.fakeuni.test. Пароль")).toBeVisible();
    const password = page.getByLabel("Ответ", { exact: true });
    await expect(password).toHaveAttribute("type", "password");
    await expect(password).toHaveAttribute("autocomplete", "current-password");
    expect(
      await page
        .getByRole("button", { name: "Открыть браузер" })
        .evaluate((button) => button.closest("form") === null),
    ).toBe(true);
    await password.fill(SECRET);
    await page.getByRole("button", { name: "Ответить" }).click();

    // Секрет не остаётся ни в поле, ни в тексте страницы, ни в хранилищах браузера.
    await expect(page.getByText("Введено")).toBeVisible();
    await expect(page.getByText(SECRET)).toHaveCount(0);
    const leaked = await page.evaluate(
      (secret) =>
        [window.localStorage, window.sessionStorage].some((storage) =>
          Object.keys(storage).some((key) => (storage.getItem(key) ?? "").includes(secret)),
        ) || document.documentElement.outerHTML.includes(secret),
      SECRET,
    );
    expect(leaked).toBe(false);

    await page.getByRole("checkbox", { name: /Алгоритмы/ }).click();
    await expect(page.getByRole("button", { name: "Открыть браузер" })).toHaveCount(0);
    await page.getByRole("checkbox", { name: /Базы данных/ }).click();
    await page.getByRole("button", { name: "Ответить" }).click();

    await expect(page.getByRole("heading", { level: 3, name: "Что подключено" })).toBeVisible();
    await expectNoHorizontalOverflow(page, "итог");
    await page.getByRole("button", { name: "Готово" }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("heading", { level: 1, name: "Сегодня" })).toBeVisible();
  });

  test("выбор native или browser входа читаем в обеих темах и доступен с клавиатуры", async ({
    page,
  }, testInfo) => {
    await startHse(page);
    await answerText(page, "anna");
    await expect(page.getByText("Вход в lk.fakeuni.test. Пароль")).toBeVisible();
    await page.getByLabel("Ответ", { exact: true }).fill(SECRET);
    for (const theme of ["light", "dark"]) {
      await page.evaluate((next) => {
        document.documentElement.dataset.theme = next;
      }, theme);
      const browser = page.getByRole("button", { name: "Открыть браузер" });
      await page.getByRole("button", { name: "Ответить" }).focus();
      await page.keyboard.press("Tab");
      await expect(browser).toBeFocused();
      await expectNoHorizontalOverflow(page, `native вход ${theme}`);
      expect(await findSmallTapTargets(page)).toEqual([]);
      expect(await findTinyText(page)).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`native-login-${theme}.png`), fullPage: true });
    }
    // UI-only terminal fixture: no answer is sent, so the pending password must disappear.
    await page.evaluate(async () => {
      const path = "/src/features/jobs/job-manager.ts";
      const { jobManager } = (await import(/* @vite-ignore */ path)) as {
        jobManager: { get: (id: string) => JobSnapshot | undefined; seed: (snapshot: JobSnapshot) => void };
      };
      const jobId = location.pathname.split("/").at(-1) ?? "";
      const snapshot = jobManager.get(jobId);
      if (!snapshot) throw new Error("Нет задания fixture");
      jobManager.seed({ ...snapshot, state: "done", result: { status: "error", detail: "login_failed" } });
      document.documentElement.style.setProperty("font-size", "200%", "important");
    });
    await expect(page.getByRole("button", { name: "Войти заново" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Ответить" })).toHaveCount(0);
    await expect(page.getByLabel("Ответ", { exact: true })).toHaveCount(0);
    for (const theme of ["light", "dark"]) {
      await page.evaluate((next) => {
        document.documentElement.dataset.theme = next;
      }, theme);
      await expectNoHorizontalOverflow(page, `terminal restart 200% ${theme}`);
      expect(await findSmallTapTargets(page)).toEqual([]);
      await page.screenshot({
        path: testInfo.outputPath(`terminal-restart-200-${theme}.png`),
        fullPage: true,
      });
    }
  });

  test("свёрнутая работа остаётся в шапке и возвращается на экран по «Открыть»", async ({ page }) => {
    await startHse(page);
    await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
    await page.getByRole("button", { name: "Свернуть" }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByText("Подключаю ВШЭ")).toBeVisible();
    await expect(page.getByText("Нужен ответ").first()).toBeVisible();
    await expectNoHorizontalOverflow(page, "шапка с работой");
    await page.getByRole("link", { name: "Открыть" }).click();
    await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
  });

  test("вуз не принял пароль: вопрос повторяется с понятным текстом", async ({ page }) => {
    await startHse(page, "wrong-password");
    await answerText(page, "anna");
    await answerText(page, "wrong");
    await expect(page.getByText(/Вуз не принял данные.*Пароль \(попытка 2 из 3\)/)).toBeVisible();
  });

  test("сбой источника: «Спросить в чате» вместо кнопки повтора, без технических кодов", async ({ page }) => {
    await startHse(page, "failure");
    await answerText(page, "anna");
    await answerText(page, "pw");
    await expect(page.getByText("Подключение не завершено.")).toBeVisible();
    await expect(page.getByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toHaveCount(0);
    expect(await page.evaluate(() => document.body.innerText)).not.toMatch(/source_unavailable|502|traceId/);
    await page.getByRole("button", { name: "Спросить в чате" }).click();
    await expect(page).toHaveURL(/\/chat$/);
  });

  test("отмена требует подтверждения; фокус стоит на безопасном «Продолжить»", async ({ page }) => {
    await startHse(page);
    await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
    await page.getByRole("button", { name: "Отменить подключение" }).click();
    const dialog = page.getByRole("alertdialog", { name: "Отменить подключение?" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Продолжить" })).toBeFocused();
    await dialog.getByRole("button", { name: "Отменить подключение" }).click();
    await expect(page.getByText(/Подключение отменено/)).toBeVisible();
  });
});

test.describe("запасной экран входа", () => {
  const REMOTE = "/styleguide#blocks";

  async function demoMessages(page: Page) {
    return page.evaluate(async () => {
      const path = "/src/features/styleguide/demo-remote.ts";
      const module = (await import(/* @vite-ignore */ path)) as {
        DemoRemoteSocket: { instances: { received: Record<string, unknown>[] }[] };
      };
      return module.DemoRemoteSocket.instances.at(-1)?.received ?? [];
    });
  }

  test("кадр рисуется, клик и ввод уходят на сервер, «Я вошёл» заканчивает успехом", async ({ page }) => {
    await page.goto(REMOTE);
    const screen = page.locator("[data-remote-screen]");
    await expect(screen).toBeVisible();
    await expect(screen).toHaveAttribute("data-remote-screen", "live");
    const canvas = screen.getByTestId("remote-canvas");
    await canvas.scrollIntoViewIfNeeded();
    // Кадр действительно нарисован: канвас не пустой.
    await expect
      .poll(() =>
        canvas.evaluate((element) => {
          const target = element as HTMLCanvasElement;
          const data = target.getContext("2d")?.getImageData(0, 0, target.width, target.height).data;
          return Boolean(data?.some((value, index) => index % 4 !== 3 && value !== 0));
        }),
      )
      .toBe(true);
    await expect(screen.getByText(/Нужна проверка «я не робот»/)).toBeVisible();

    const box = await canvas.boundingBox();
    if (!box) throw new Error("нет канваса");
    // Поле «Логин» у демонстрационного сервера — вторая пятая часть высоты.
    await page.mouse.click(box.x + box.width / 2, box.y + box.height * 0.28);
    await page.keyboard.type("anna");
    await expect
      .poll(async () => (await demoMessages(page)).filter((message) => message.t === "key").length)
      .toBeGreaterThan(0);
    const messages = await demoMessages(page);
    expect(messages.some((message) => message.t === "mouse")).toBe(true);
    expect(messages.some((message) => message.t === "resize")).toBe(true);

    await screen.getByRole("button", { name: "Я вошёл" }).click();
    await expect(screen.getByText("Вход выполнен. Продолжаю подключение.")).toBeVisible();
    await expectNoHorizontalOverflow(page, "запасной экран");
  });
});

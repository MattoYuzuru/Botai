import { expect, type Page, test } from "@playwright/test";
import type { JobSnapshot } from "../src/features/jobs/job-state";
import {
  expectNoHorizontalOverflow,
  expectSheetReady,
  findSmallTapTargets,
  findTinyText,
  open,
} from "./support";

const SECRET = "correct-horse-battery-staple";
const URL_QUESTION = "Пришлите ссылку на источник, который хотите подключить.";
async function start(page: Page, variant = "") {
  await open(page, `/connect?pace=0${variant ? `&job=${variant}` : ""}`, "empty");
  await expect(page).toHaveURL(/\/chat\?thread=/);
  await expect(page.getByText(URL_QUESTION)).toBeVisible();
}
async function send(page: Page, text: string) {
  const composer = page.getByRole("textbox", { name: "Сообщение агенту" });
  await expect(composer).toBeEnabled();
  await composer.fill(text);
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
}
async function native(page: Page, variant = "") {
  await start(page, variant);
  await send(page, "https://lk.fakeuni.test/");
  await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
}
async function answer(page: Page, text: string) {
  await page.getByLabel("Ответ", { exact: true }).fill(text);
  await page.getByRole("button", { name: "Ответить", exact: true }).click();
}

async function expectChatReflow(page: Page) {
  const expectedWidth = page.viewportSize()?.width;
  if (!expectedWidth) throw new Error("Нет viewport");
  const layout = await page.evaluate(() => {
    const rect = (element: Element) => {
      const box = element.getBoundingClientRect();
      return {
        left: box.left,
        right: box.right,
        width: box.width,
        height: box.height,
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
      };
    };
    const viewport = document.querySelector("[data-chat-transcript] [data-radix-scroll-area-viewport]");
    const heading = document.querySelector("header h1");
    if (!viewport || !heading) throw new Error("Нет chat layout");
    const box = viewport.getBoundingClientRect();
    const style = getComputedStyle(viewport);
    return {
      width: innerWidth,
      visualWidth: visualViewport?.width ?? innerWidth,
      viewport: rect(viewport),
      contentLeft: box.left + Number.parseFloat(style.paddingLeft),
      contentRight: box.right - Number.parseFloat(style.paddingRight),
      chain: [
        ...viewport.querySelectorAll(
          ":scope > div, [data-timeline], [data-timeline] > li, [data-role], [data-role] > div:first-child",
        ),
      ].map(rect),
      controls: [
        ...viewport.querySelectorAll(
          "[data-chat-input] button, [data-chat-input] input, [data-connection-failure] button",
        ),
      ].map(rect),
      heading: rect(heading),
      headerControls: [...document.querySelectorAll("header button")].map(rect),
      nav: [...document.querySelectorAll('nav[aria-label="Основная навигация"] a')].map(rect),
    };
  });
  expect(layout.width).toBe(expectedWidth);
  expect(layout.visualWidth).toBe(expectedWidth);
  expect(layout.viewport.left).toBeGreaterThanOrEqual(0);
  expect(layout.viewport.right).toBeLessThanOrEqual(expectedWidth);
  expect(layout.viewport.scrollWidth).toBeLessThanOrEqual(layout.viewport.clientWidth);
  for (const box of [...layout.chain, ...layout.controls]) {
    expect(box.left).toBeGreaterThanOrEqual(layout.contentLeft);
    expect(box.right).toBeLessThanOrEqual(layout.contentRight);
  }
  for (const box of layout.chain) expect(box.scrollWidth).toBeLessThanOrEqual(box.clientWidth);
  expect(layout.heading.scrollWidth).toBeLessThanOrEqual(layout.heading.clientWidth);
  expect(layout.nav).toHaveLength(5);
  for (const box of [...layout.headerControls, ...layout.nav]) {
    expect(box.left).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(expectedWidth);
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
  }
}

test.describe("подключение в общем чате", () => {
  test("первый вопрос стабилен до/после ответа, хронология идёт вниз без дубликата", async ({ page }) => {
    await start(page);
    const question = page.getByText(URL_QUESTION);
    await question.evaluate((element) => {
      element.dataset.identity = "same-question";
    });
    await send(page, "https://lk.fakeuni.test/");
    await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
    await expect(question).toHaveAttribute("data-identity", "same-question");
    await expect(question).toHaveCount(1);
    const history = (await page.getByRole("list", { name: "Сообщения" }).textContent()) ?? "";
    expect(history.indexOf(URL_QUESTION)).toBeLessThan(history.indexOf("https://lk.fakeuni.test/"));
    expect(history.indexOf("https://lk.fakeuni.test/")).toBeLessThan(
      history.indexOf("Вход в lk.fakeuni.test. Логин"),
    );
    await expect(page.getByRole("textbox", { name: "Сообщение агенту" })).toBeDisabled();
  });

  test("native login → secret → выбор → итог остаются в том же чате; секрет не сохраняется", async ({
    page,
  }) => {
    await native(page);
    const thread = page.url();
    await expect(page.getByText("Вход через поля BotAI")).toBeVisible();
    await expect(page.getByRole("button", { name: "Открыть браузер" })).toBeVisible();
    await answer(page, "anna");
    await expect(page.getByText("Вход в lk.fakeuni.test. Пароль")).toBeVisible();
    const password = page.getByLabel("Ответ", { exact: true });
    await expect(password).toHaveAttribute("type", "password");
    await expect(password).toHaveAttribute("autocomplete", "current-password");
    expect(
      await page
        .getByRole("button", { name: "Открыть браузер" })
        .evaluate((button) => button.closest("form") === null),
    ).toBe(true);
    await answer(page, SECRET);
    await expect(page.getByRole("checkbox", { name: /Алгоритмы/ })).toBeVisible();
    const leaked = await page.evaluate(
      (secret) =>
        [window.localStorage, window.sessionStorage].some((storage) =>
          Object.keys(storage).some((key) => (storage.getItem(key) ?? "").includes(secret)),
        ) || document.documentElement.outerHTML.includes(secret),
      SECRET,
    );
    expect(leaked).toBe(false);
    await expect(page.getByRole("button", { name: "Открыть браузер" })).toHaveCount(0);
    await page.getByRole("checkbox", { name: /Алгоритмы/ }).check();
    await page.getByRole("button", { name: "Ответить", exact: true }).click();
    await expect(page.getByRole("heading", { level: 3, name: "Что подключено" })).toBeVisible();
    await expect(page).toHaveURL(thread);
    await expect(page.getByRole("textbox", { name: "Сообщение агенту" })).toBeEnabled();
    await expectNoHorizontalOverflow(page, "итог в чате");
  });

  test("контекст каталога и native editor доступны на телефонах/ноутбуке в обеих темах", async ({
    page,
  }, testInfo) => {
    for (const width of [320, 390, 430, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      await start(page);
      await page.getByRole("button", { name: "Источник и подключения" }).click();
      await expectSheetReady(page, "Источник и подключения");
      await page.getByRole("combobox", { name: "Вуз" }).click();
      await expect(page.getByRole("option", { name: /Высшая школа экономики/ })).toBeVisible();
      await expectNoHorizontalOverflow(page, `каталог ${width}`);
      await page.getByRole("option", { name: /Высшая школа экономики/ }).click();
      await page
        .getByRole("dialog", { name: "Источник и подключения" })
        .getByRole("button", { name: "Закрыть" })
        .click();
      await send(page, "https://lk.fakeuni.test/");
      await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
      for (const theme of ["light", "dark"]) {
        await page.evaluate((next) => {
          document.documentElement.dataset.theme = next;
        }, theme);
        await expectNoHorizontalOverflow(page, `native ${width} ${theme}`);
        expect(await findSmallTapTargets(page)).toEqual([]);
        expect(await findTinyText(page)).toEqual([]);
        const fontSize = await page
          .getByLabel("Ответ", { exact: true })
          .evaluate((input) => Number.parseFloat(getComputedStyle(input).fontSize));
        expect(fontSize).toBeGreaterThanOrEqual(16);
        if (width >= 1280) {
          for (const box of await page
            .locator("[data-role] > div:first-child")
            .evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().width)))
            expect(box).toBeLessThanOrEqual(544);
        }
        await expect(page.locator("[data-connection-failure]")).toHaveCount(0);
        await page.screenshot({ path: testInfo.outputPath(`connect-${width}-${theme}.png`), fullPage: true });
      }
    }
  });

  test("native/browser option и terminal restart не переполняются при 200% текста", async ({
    page,
  }, testInfo) => {
    await native(page);
    await answer(page, "anna");
    await expect(page.getByText("Вход в lk.fakeuni.test. Пароль")).toBeVisible();
    await page.getByLabel("Ответ", { exact: true }).fill(SECRET);
    await page.getByRole("button", { name: "Ответить", exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "Открыть браузер" })).toBeFocused();
    await page.evaluate(() => {
      document.documentElement.style.setProperty("font-size", "200%", "important");
    });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((next) => {
        document.documentElement.dataset.theme = next;
      }, theme);
      await expectNoHorizontalOverflow(page, `native 200% ${theme}`);
      expect(await findSmallTapTargets(page)).toEqual([]);
      await expectChatReflow(page);
      await page.screenshot({ path: testInfo.outputPath(`native-200-${theme}.png`), fullPage: true });
    }
    // UI-only external failure: no answer is submitted and no credential is fabricated.
    await page.evaluate(async () => {
      const managerPath = "/src/features/jobs/job-manager.ts";
      const activePath = "/src/features/jobs/active-job.ts";
      const { jobManager } = (await import(/* @vite-ignore */ managerPath)) as {
        jobManager: { get: (id: string) => JobSnapshot | undefined; seed: (job: JobSnapshot) => void };
      };
      const { useActiveJob } = (await import(/* @vite-ignore */ activePath)) as {
        useActiveJob: { getState: () => { job: { jobId: string } | null } };
      };
      const id = useActiveJob.getState().job?.jobId;
      const job = id ? jobManager.get(id) : undefined;
      if (!job) throw new Error("missing active fixture job");
      jobManager.seed({
        ...job,
        lastEventId: "999",
        state: "done",
        result: { status: "error", detail: "login_failed" },
      });
    });
    await expect(page.getByRole("button", { name: "Войти заново" })).toBeVisible();
    await expect(page.getByLabel("Ответ", { exact: true })).toHaveCount(0);
    for (const theme of ["light", "dark"]) {
      await page.evaluate((next) => {
        document.documentElement.dataset.theme = next;
      }, theme);
      await expectNoHorizontalOverflow(page, `terminal 200% ${theme}`);
      const geometry = await page.locator("[data-connection-failure]").evaluate((element) => {
        const parent = element.getBoundingClientRect();
        return {
          left: parent.left,
          right: parent.right,
          children: [...element.querySelectorAll("article, article > div:first-child")].map((child) => {
            const box = child.getBoundingClientRect();
            return {
              left: box.left,
              right: box.right,
              scrollWidth: child.scrollWidth,
              clientWidth: child.clientWidth,
            };
          }),
        };
      });
      expect(geometry.children).toHaveLength(2);
      for (const child of geometry.children) {
        expect(child.left).toBeGreaterThanOrEqual(geometry.left);
        expect(child.right).toBeLessThanOrEqual(geometry.right);
        expect(child.scrollWidth).toBeLessThanOrEqual(child.clientWidth);
      }
      await expect
        .poll(() =>
          page
            .getByRole("textbox", { name: "Сообщение агенту" })
            .evaluate((element) => element.scrollHeight - element.clientHeight),
        )
        .toBeLessThanOrEqual(0);
      const box = await page.getByRole("button", { name: "Войти заново" }).boundingBox();
      expect(box).not.toBeNull();
      if (box) {
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()?.width ?? 0);
      }
      expect(await findSmallTapTargets(page)).toEqual([]);
      await expectChatReflow(page);
      await page.screenshot({ path: testInfo.outputPath(`terminal-200-${theme}.png`), fullPage: true });
    }
  });

  test("composer пересчитывает пустой placeholder при его смене и 200%, длинный текст сохраняет предел строк", async ({
    page,
  }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await native(page);
    const originalViewport = page.viewportSize();
    if (!originalViewport) throw new Error("Нет viewport");
    await page.evaluate(() => document.documentElement.style.setProperty("font-size", "200%", "important"));
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 844 });
      await expectChatReflow(page);
      if (width === 320)
        await page.screenshot({ path: testInfo.outputPath("native-320-200.png"), fullPage: true });
    }
    await page.evaluate(() => document.documentElement.style.removeProperty("font-size"));
    await page.setViewportSize(originalViewport);
    await page.evaluate(async () => {
      const path = "/src/features/jobs/active-job.ts";
      const { useActiveJob } = (await import(/* @vite-ignore */ path)) as {
        useActiveJob: { getState: () => { job: { jobId: string } | null } };
      };
      const id = useActiveJob.getState().job?.jobId;
      if (!id) throw new Error("Нет active fixture job");
      const response = await fetch(`/api/v1/jobs/${id}/cancel`, { method: "POST" });
      if (!response.ok) throw new Error("Fixture cancel failed");
    });
    const composer = page.getByRole("textbox", { name: "Сообщение агенту" });
    await expect(composer).toBeEnabled();
    await expect(composer).toHaveAttribute("placeholder", "Ответьте агенту или уточните задачу");
    await expect
      .poll(() => composer.evaluate((element) => element.scrollHeight - element.clientHeight))
      .toBeLessThanOrEqual(0);
    await page.evaluate(() => document.documentElement.style.setProperty("font-size", "200%", "important"));
    await expect
      .poll(() => composer.evaluate((element) => element.scrollHeight - element.clientHeight))
      .toBeLessThanOrEqual(0);
    await composer.fill("Длинная строка для проверки переноса и прокрутки. ".repeat(80));
    const long = await composer.evaluate((element) => ({
      height: element.clientHeight,
      content: element.scrollHeight,
      cap: Number.parseFloat(getComputedStyle(element).lineHeight) * 6,
      overflow: getComputedStyle(element).overflowY,
    }));
    expect(long.height).toBeLessThanOrEqual(long.cap);
    expect(long.content).toBeGreaterThan(long.height);
    expect(long.overflow).toBe("auto");
    expect(errors).toEqual([]);
  });

  test("фоновая операция возвращается через canonical chat notification", async ({ page }) => {
    await native(page);
    const thread = page.url();
    await page.getByRole("link", { name: "Сегодня", exact: true }).click();
    await expect(page.getByText("Нужен ответ")).toBeVisible();
    await page.getByRole("link", { name: "Открыть", exact: true }).click();
    await expect(page).toHaveURL(thread);
    await expect(page.getByRole("button", { name: "Открыть браузер" })).toBeVisible();
  });

  test("непринятый пароль создаёт новый вопрос, прежние controls readonly", async ({ page }) => {
    await native(page, "wrong-password");
    await answer(page, "anna");
    await expect(page.getByText("Вход в lk.fakeuni.test. Пароль")).toBeVisible();
    await answer(page, SECRET);
    await expect(page.getByText(/попытка 2 из 3/)).toBeVisible();
    await expect(page.getByLabel("Ответ", { exact: true })).toHaveCount(1);
    await expect(page.getByLabel("Ответ", { exact: true })).toHaveValue("");
  });

  test("сбой источника оставляет понятную причину и бизнес-действие нового входа", async ({ page }) => {
    await native(page, "failure");
    await answer(page, "anna");
    await expect(page.getByText("Вход в lk.fakeuni.test. Пароль")).toBeVisible();
    await answer(page, SECRET);
    await expect(page.getByText(/Сайт вуза не ответил/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Войти заново" })).toBeVisible();
    await expect(page.getByLabel("Ответ", { exact: true })).toHaveCount(0);
    expect(await page.locator("body").textContent()).not.toContain("source_unavailable");
  });

  test("отмена с безопасным фокусом остаётся доступной в контексте чата", async ({ page }) => {
    await native(page);
    await page.getByRole("button", { name: "Источник и подключения" }).click();
    await page.getByRole("button", { name: "Отменить подключение", exact: true }).click();
    const dialog = page.getByRole("alertdialog", { name: "Отменить подключение?" });
    await expect(dialog.getByRole("button", { name: "Продолжить" })).toBeFocused();
    await dialog.getByRole("button", { name: "Отменить подключение", exact: true }).click();
    await expect(page.getByText(/Подключение отменено/)).toBeVisible();
  });
});

test("remote canvas сохраняет рисование, CSS input и явное завершение", async ({ page }) => {
  await page.goto("/styleguide#blocks");
  const screen = page.locator("[data-remote-screen]");
  await expect(screen).toHaveAttribute("data-remote-screen", "live");
  const canvas = screen.getByTestId("remote-canvas");
  await canvas.scrollIntoViewIfNeeded();
  await expect
    .poll(() =>
      canvas.evaluate((element) => {
        const target = element as HTMLCanvasElement;
        return Boolean(
          target
            .getContext("2d")
            ?.getImageData(0, 0, target.width, target.height)
            .data.some((value, index) => index % 4 !== 3 && value !== 0),
        );
      }),
    )
    .toBe(true);
  await expect(screen.getByText(/Нужна проверка «я не робот»/)).toBeVisible();
  const box = await canvas.boundingBox();
  if (!box) throw new Error("missing canvas");
  await page.mouse.click(box.x + box.width / 2, box.y + box.height * 0.28);
  await page.keyboard.type("anna");
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const path = "/src/features/styleguide/demo-remote.ts";
        const module = (await import(/* @vite-ignore */ path)) as {
          DemoRemoteSocket: { instances: { received: Record<string, unknown>[] }[] };
        };
        const messages = module.DemoRemoteSocket.instances.at(-1)?.received ?? [];
        return (
          messages.some((item) => item.t === "key") &&
          messages.some((item) => item.t === "mouse") &&
          messages.some((item) => item.t === "resize")
        );
      }),
    )
    .toBe(true);
  await screen.getByRole("button", { name: "Я вошёл" }).click();
  await expect(screen.getByText("Вход выполнен. Продолжаю подключение.")).toBeVisible();
  await expectNoHorizontalOverflow(page, "remote screen");
});

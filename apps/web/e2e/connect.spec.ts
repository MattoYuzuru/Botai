import { expect, type Page, test } from "@playwright/test";
import { expectNoHorizontalOverflow, findSmallTapTargets, findTinyText, open } from "./support";

/**
 * Подключение вуза в общем чате: один тред — один таймлайн. «Подключи ЦУ» запускает задачу в этом же треде,
 * вопросы входа стоят у поля ввода, секреты идут через `jobs/answers`, итог остаётся в ленте.
 */

const SECRET = "correct-horse-battery-staple";
const LOGIN = "Вход в lk.fakeuni.test. Логин";
const PASSWORD = "Вход в lk.fakeuni.test. Пароль";
const composer = (page: Page) => page.getByRole("textbox", { name: "Сообщение агенту" });
const pending = (page: Page) => page.locator("[data-pending-questions]");

async function start(page: Page, variant = "") {
  await open(page, `/chat?thread=new&pace=0${variant ? `&job=${variant}` : ""}`, "empty");
  await expect(page.getByRole("heading", { level: 2, name: "Спросите про пары и сроки" })).toBeVisible();
}
async function send(page: Page, text: string) {
  await expect(composer(page)).toBeEnabled();
  await composer(page).fill(text);
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
}
async function native(page: Page, variant = "") {
  await start(page, variant);
  await send(page, "Подключи ЦУ https://new.fakeuni.test");
  await expect(page.getByText(LOGIN)).toBeVisible();
}
async function answer(page: Page, text: string) {
  await pending(page).getByLabel("Ответ", { exact: true }).fill(text);
  await pending(page).getByRole("button", { name: "Ответить", exact: true }).click();
}

test.describe("подключение в общем чате", () => {
  test("«Подключи ЦУ»: задача в этом же треде, вход → пароль-секрет → выбор → итог; секрет нигде не сохраняется", async ({
    page,
  }) => {
    await native(page);
    const thread = page.url();
    // Карточка задачи — в ленте этого треда; вопрос входа стоит у поля ввода, а не отдельным экраном.
    const card = page.locator("[data-task-card]");
    await expect(card).toContainText("Подключение ЦУ");
    await expect(card).toContainText("Ждёт вас");
    await expect(page.locator("[data-timeline]")).toHaveCount(1);
    // Поле не выключено: агенту можно написать, пока задача ждёт вход.
    await expect(composer(page)).toBeEnabled();
    await expect(composer(page)).toHaveAttribute("placeholder", "Ответьте в поле выше или напишите агенту");
    await expect(page.getByText("Вход через поля BotAI")).toBeVisible();
    await expect(page.getByRole("button", { name: "Открыть браузер" })).toBeVisible();
    // Живой блок мышления — последний в ленте, под всем, что появилось за время работы задачи.
    const turns = page.locator("[data-timeline] > li");
    await expect(turns.last()).toHaveAttribute("data-turn", "thinking");

    await answer(page, "anna");
    await expect(page.getByText(PASSWORD)).toBeVisible();
    const password = pending(page).getByLabel("Ответ", { exact: true });
    await expect(password).toHaveAttribute("type", "password");
    await expect(password).toHaveAttribute("autocomplete", "current-password");
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
    await page.getByRole("checkbox", { name: /Алгоритмы/ }).check();
    await page.getByRole("button", { name: "Ответить", exact: true }).click();
    // Одно сообщение-подтверждение: что собрано словами, ближайшие пары и вопрос с кнопками — вместе.
    await expect(pending(page)).toContainText("42 занятия с 26 октября по 20 декабря — 8 недель");
    await expect(pending(page)).toContainText("Ближайшие пары:");
    await expect(pending(page)).toContainText("Всё верно?");
    await expectNoHorizontalOverflow(page, "подтверждение в чате");
    await pending(page).getByRole("button", { name: "Да, всё верно" }).click();
    await expect(page.getByText(/^Готово! Расписание Фейкуни подключено/)).toBeVisible();
    // Работа закончилась: свёрнутый «Думал» стоит прямо над итоговым ответом, а не высоко над ним.
    const closing = turns.filter({ hasText: "Готово! Расписание Фейкуни подключено" });
    await expect(closing.locator("xpath=preceding-sibling::li[1]")).toHaveAttribute("data-turn", "thinking");
    await expect(closing.locator("xpath=preceding-sibling::li[1]")).toContainText("Думал");
    await expect(turns.last()).toHaveAttribute("data-turn", "block");
    // Ответ человека в ленте — словами, под цитатой вопроса.
    const reply = page.locator("[data-role='user']", { hasText: "Да, всё верно" });
    await expect(reply.locator("[data-message-quote]")).toContainText("Всё верно?");
    await expect(page).toHaveURL(thread);
    await expect(composer(page)).toBeEnabled();
    await expect(pending(page)).toHaveCount(0);
    await expectNoHorizontalOverflow(page, "итог в чате");
  });

  test("пока задача ждёт пароль, агенту можно написать: он отвечает, защищённый вопрос остаётся", async ({
    page,
  }) => {
    await native(page);
    await answer(page, "anna");
    await expect(page.getByText(PASSWORD)).toBeVisible();
    await expect(composer(page)).toBeEnabled();
    await expect(composer(page)).toHaveAttribute("placeholder", "Пароль вводите в защищённое поле выше");
    await send(page, "Что ты делаешь?");
    await expect(page.getByText(/^Не знаю:/)).toBeVisible();
    await expect(pending(page).getByLabel("Ответ", { exact: true })).toHaveAttribute("type", "password");
  });

  test("ответ на вопрос задачи текстом из поля ввода: ссылка в диалоге уходит задаче, а не новому ходу агента", async ({
    page,
  }) => {
    await start(page);
    await send(page, "Подключи ЦУ");
    await expect(page.getByText("Пришлите ссылку на источник, который хотите подключить.")).toBeVisible();
    await expect(composer(page)).toBeEnabled();
    await expect(composer(page)).toHaveAttribute("placeholder", "Ответьте агенту или уточните задачу");
    await send(page, "https://new.fakeuni.test");
    await expect(page.getByText(LOGIN)).toBeVisible();
    const text = (await page.getByRole("list", { name: "Сообщения" }).textContent()) ?? "";
    expect(text.indexOf("Пришлите ссылку")).toBeLessThan(text.indexOf("https://new.fakeuni.test"));
    // Новый вопрос задачи с вводом стоит у поля ввода, а не в ленте.
    await expect(pending(page).getByText(LOGIN)).toBeVisible();
  });

  test("повторная просьба: «уже подключено» со статусом и быстрыми ответами, вторая задача не запускается", async ({
    page,
  }) => {
    await open(page, "/chat?thread=new&pace=0", "connected");
    await send(page, "Подключи ЦУ https://my.fakeuni.test");
    await expect(page.getByText(/уже подключён/)).toBeVisible();
    await expect(page.getByRole("group", { name: "Быстрые ответы" }).getByRole("button")).toHaveText([
      "Перепроверить",
      "Войти заново",
    ]);
    await expect(page.locator("[data-task-card]")).toHaveCount(0);
  });

  test("ошибка отправки возвращает текст в поле, отвергнутый текст не остаётся в ленте", async ({ page }) => {
    await open(page, "/chat?thread=new&pace=0", "connected");
    await page.evaluate(() => {
      const original = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const request = input instanceof Request ? input : new Request(input, init);
        if (
          request.method === "POST" &&
          /\/chat\/threads\/[^/]+\/messages$/.test(new URL(request.url).pathname)
        )
          return Promise.resolve(
            new Response(JSON.stringify({ code: "validation", detail: "Сообщение отклонено" }), {
              status: 422,
              headers: { "Content-Type": "application/problem+json" },
            }),
          );
        return original(input, init);
      };
    });
    const text = "Что в расписании?";
    await send(page, text);
    await expect(composer(page)).toHaveValue(text);
    await expect(page.getByRole("list", { name: "Сообщения" }).getByText(text, { exact: true })).toHaveCount(
      0,
    );
  });

  test("непринятый пароль создаёт новый вопрос, прежние поля остаются в истории", async ({ page }) => {
    await native(page, "wrong-password");
    await answer(page, "anna");
    await expect(page.getByText(PASSWORD, { exact: true })).toBeVisible();
    await answer(page, SECRET);
    await expect(page.getByText(/попытка 2 из 3/)).toBeVisible();
    await expect(pending(page).getByLabel("Ответ", { exact: true })).toHaveCount(1);
    await expect(pending(page).getByLabel("Ответ", { exact: true })).toHaveValue("");
  });

  test("вход по подтверждению на телефоне: карточка ожидания у поля ввода, потом разговор продолжается", async ({
    page,
  }) => {
    await native(page, "push");
    await answer(page, "anna");
    await answer(page, SECRET);
    await expect(page.getByRole("checkbox", { name: /Алгоритмы/ })).toBeVisible();
    await expect(page.getByText(/Подтвердите вход в приложении на телефоне/).first()).toBeVisible();
  });

  test("запись по подтверждению: карточка предложения у поля ввода, одобрение завершает подключение", async ({
    page,
  }) => {
    await native(page, "approval");
    await answer(page, "anna");
    await answer(page, SECRET);
    await page.getByRole("checkbox", { name: /Алгоритмы/ }).check();
    await page.getByRole("button", { name: "Ответить", exact: true }).click();
    await expect(pending(page).getByText("Записаться на консультацию по базам данных")).toBeVisible();
    await pending(page).getByRole("button", { name: "Подтвердить" }).click();
    await pending(page).getByRole("button", { name: "Да, всё верно" }).click();
    await expect(page.getByText(/^Готово! Расписание Фейкуни подключено/)).toBeVisible();
  });

  test("сбой источника: понятная причина и «Войти заново», кодов в тексте нет", async ({ page }) => {
    await native(page, "failure");
    await answer(page, "anna");
    await answer(page, SECRET);
    await expect(page.getByText(/Сайт вуза не ответил/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Войти заново" })).toBeVisible();
    await expect(pending(page)).toHaveCount(0);
    expect(await page.locator("body").textContent()).not.toContain("source_unavailable");
    await expect(composer(page)).toBeEnabled();
  });

  test("задачу можно остановить из карточки; фокус диалога на безопасной кнопке", async ({ page }) => {
    await native(page);
    await page.getByRole("button", { name: "Отменить подключение", exact: true }).click();
    const dialog = page.getByRole("alertdialog", { name: "Отменить подключение?" });
    await expect(dialog.getByRole("button", { name: "Продолжить" })).toBeFocused();
    await dialog.getByRole("button", { name: "Отменить подключение", exact: true }).click();
    await expect(page.getByText(/Остановлено/)).toBeVisible();
  });

  test("фоновая операция видна на других экранах, «Открыть» возвращает в тот же тред", async ({ page }) => {
    await native(page);
    const thread = page.url();
    await page.getByRole("link", { name: "Сегодня", exact: true }).click();
    await expect(page.getByText("Нужен ответ")).toBeVisible();
    await page.getByRole("link", { name: "Открыть", exact: true }).click();
    await expect(page).toHaveURL(thread);
    await expect(page.getByRole("button", { name: "Открыть браузер" })).toBeVisible();
  });

  test("вопрос входа и поле ввода не переполняют экран на 320–430 px и ноутбуке, обе темы", async ({
    page,
  }, testInfo) => {
    for (const width of [320, 390, 430, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      await native(page);
      for (const theme of ["light", "dark"]) {
        await page.evaluate((next) => {
          document.documentElement.dataset.theme = next;
        }, theme);
        await expectNoHorizontalOverflow(page, `вход ${width} ${theme}`);
        expect(await findSmallTapTargets(page)).toEqual([]);
        expect(await findTinyText(page)).toEqual([]);
        const fontSize = await pending(page)
          .getByLabel("Ответ", { exact: true })
          .evaluate((input) => Number.parseFloat(getComputedStyle(input).fontSize));
        expect(fontSize).toBeGreaterThanOrEqual(16);
        const geometry = await page.evaluate(() => {
          const column = document.querySelector("[data-chat-transcript]")?.getBoundingClientRect();
          const parts = ["[data-pending-questions]", "form[data-composer]", "[data-timeline]"].map(
            (selector) => {
              const box = document.querySelector(selector)?.getBoundingClientRect();
              return { selector, left: box?.left ?? 0, right: box?.right ?? 0 };
            },
          );
          return { column: { left: column?.left ?? 0, right: column?.right ?? 0 }, parts };
        });
        for (const part of geometry.parts) {
          expect(part.left, `${part.selector} слева на ${width}`).toBeGreaterThanOrEqual(
            geometry.column.left,
          );
          expect(part.right, `${part.selector} справа на ${width}`).toBeLessThanOrEqual(
            geometry.column.right,
          );
        }
        await page.screenshot({ path: testInfo.outputPath(`connect-${width}-${theme}.png`) });
      }
    }
  });

  test("крупный текст 200%: вопрос входа, «Открыть браузер» и итоговый сбой не переполняют экран", async ({
    page,
  }, testInfo) => {
    await native(page);
    await page.evaluate(() => document.documentElement.style.setProperty("font-size", "200%", "important"));
    for (const theme of ["light", "dark"]) {
      await page.evaluate((next) => {
        document.documentElement.dataset.theme = next;
      }, theme);
      await expectNoHorizontalOverflow(page, `вход 200% ${theme}`);
      expect(await findSmallTapTargets(page)).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`connect-200-${theme}.png`) });
    }
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

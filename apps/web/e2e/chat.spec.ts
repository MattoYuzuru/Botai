import { expect, type Page, test } from "@playwright/test";
import { expectNoHorizontalOverflow, findSmallTapTargets, findTinyText, open } from "./support";

/**
 * Чат в настоящем браузере на моках: раскладка (поле ввода над нижней панелью, страница не прокручивается),
 * ответ агента с источниками, внутренние ссылки, шкала контекста, запрос на запись и очередь без сети.
 */

const composer = (page: Page) => page.getByRole("textbox", { name: "Сообщение агенту" });

async function ask(page: Page, text: string) {
  await composer(page).fill(text);
  await page.getByRole("button", { name: "Отправить" }).click();
}

const openChat = (page: Page, path = "/chat?pace=0") => open(page, path, "connected");

/** Лента сообщений: название чата в шапке повторяет вопрос, поэтому ищем текст только в ленте. */
const timeline = (page: Page) => page.getByRole("list", { name: "Сообщения" });

test.describe("раскладка чата", () => {
  test("поле ввода над нижней панелью, страница не прокручивается, кегль поля 16 px", async ({ page }) => {
    await openChat(page);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(composer(page)).toBeVisible();
    const layout = await page.evaluate(() => {
      const box = (selector: string) => document.querySelector(selector)?.getBoundingClientRect();
      const dock = box("[data-chat-dock]");
      const nav = box("nav[aria-label='Основная навигация']");
      const header = box("header");
      return {
        dockBottom: dock?.bottom ?? 0,
        navTop: nav?.top ?? 0,
        navLeft: nav?.left ?? 0,
        navWidth: nav?.width ?? 0,
        headerBottom: header?.bottom ?? 0,
        dockTop: dock?.top ?? 0,
        scrollable: document.documentElement.scrollHeight - window.innerHeight,
        fontSize: Number.parseFloat(getComputedStyle(document.querySelector("textarea") as Element).fontSize),
        viewportHeight: window.innerHeight,
      };
    });
    expect(layout.fontSize).toBeGreaterThanOrEqual(16);
    expect(layout.dockTop).toBeGreaterThan(layout.headerBottom);
    expect(layout.scrollable).toBeLessThanOrEqual(1);
    // Телефон: нижняя панель закрывает низ, поле над ней. Ноутбук: панель слева, поле до низа экрана.
    if (layout.navWidth > 200) expect(layout.dockBottom).toBeLessThanOrEqual(layout.viewportHeight);
    else expect(layout.dockBottom).toBeLessThanOrEqual(layout.navTop + 1);
  });

  test("нет горизонтальной прокрутки на 320, 390 и 430 px, зоны нажатия и кегль в норме", async ({
    page,
  }) => {
    for (const width of [320, 390, 430]) {
      await page.setViewportSize({ width, height: 800 });
      await openChat(page);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(composer(page)).toBeVisible();
      await expectNoHorizontalOverflow(page, `чат ${width}`);
    }
    expect(await findSmallTapTargets(page)).toEqual([]);
    expect(await findTinyText(page)).toEqual([]);
  });

  test("длинная переписка прокручивается внутри ленты, а не страницей", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    for (const question of ["Что сдавать на неделе?", "Какая следующая пара?", "Расскажи анекдот"]) {
      await ask(page, question);
      await expect(timeline(page).getByText(question, { exact: true })).toBeVisible();
      await expect(composer(page)).toBeEnabled();
    }
    // Последний ответ дорисован, лента догнала его.
    await expect(timeline(page).getByText(/^Не знаю:/)).toBeVisible();
    await page.waitForTimeout(300);
    const state = await page.evaluate(() => {
      const viewport = document.querySelector("[data-radix-scroll-area-viewport]") as HTMLElement;
      return {
        page: document.documentElement.scrollHeight - window.innerHeight,
        listScrollable: viewport.scrollHeight - viewport.clientHeight,
        atBottom: viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight,
      };
    });
    expect(state.page).toBeLessThanOrEqual(1);
    // Лента держится у последнего сообщения.
    expect(state.atBottom).toBeLessThanOrEqual(4);
  });
});

test.describe("ответ агента", () => {
  test("история, источники и внутренняя ссылка: подключение открывается в «Учёбе»", async ({ page }) => {
    await openChat(page);
    await expect(timeline(page).getByText("Что сегодня по расписанию?", { exact: true })).toBeVisible();
    const external = page.getByRole("link", { name: /lk\.fakeuni\.test/ });
    await expect(external).toHaveAttribute("target", "_blank");
    const internal = page.getByRole("link", { name: /Открыть подключение в «Учёбе»/ });
    await internal.click();
    await expect(page).toHaveURL(/\/study\?connection=/);
  });

  test("шкала контекста: плоская полоса в шапке, шит с сегментами и источниками", async ({ page }) => {
    await openChat(page);
    const button = page.getByRole("button", { name: /Контекст агента: занято/ });
    await expect(button).toBeVisible();
    await button.click();
    const sheet = page.getByRole("dialog", { name: "Контекст агента" });
    await expect(sheet.getByRole("list", { name: "Из чего состоит контекст" })).toBeVisible();
    await expect(sheet.getByText("Правила и навыки")).toBeVisible();
    await expect(sheet.getByRole("heading", { name: "Источники ответа" })).toBeVisible();
    // Полоса одна и плоская: сегменты внутри одного трека, без радиусов у соседей.
    const segments = await sheet.locator("[data-context-bar] [data-segment]").count();
    expect(segments).toBeGreaterThanOrEqual(3);
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
  });

  test("подсказка отправляет вопрос; ответ с источником и без «Не уверен»", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await page.getByRole("list", { name: "Подсказки" }).getByRole("button").first().click();
    await expect(page.getByRole("link", { name: /Открыть подключение в «Учёбе»/ }).first()).toBeVisible();
    await expect(page.getByText("Не уверен")).toHaveCount(0);
  });

  test("«Не знаю» помечено «Не уверен»", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "Расскажи анекдот");
    await expect(page.getByText("Не уверен")).toBeVisible();
    await expect(page.getByText(/^Не знаю:/)).toBeVisible();
  });

  test("запрос на запись: вопрос, карточка с методом, хостом и полями, одобрение", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "Мне нужна справка");
    await expect(page.getByText("Какая справка нужна?")).toBeVisible();
    await expect(composer(page)).toBeDisabled();
    await expect(composer(page)).toHaveAttribute("placeholder", "Ответьте на вопрос выше");
    await page.getByRole("radio", { name: "О стипендии" }).click();
    await page.getByRole("button", { name: "Ответить" }).click();
    await expect(page.getByText("Подать заявку на справку")).toBeVisible();
    await expect(page.getByText(/POST\s+lk\.fakeuni\.test/)).toBeVisible();
    await page.getByRole("button", { name: "Подтвердить" }).click();
    await expect(page.getByText(/Заявка отправлена/)).toBeVisible();
    await expect(composer(page)).toBeEnabled();
  });

  test("готовый текст для ручной отправки остаётся в ленте после ответа", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "Подай заявку вручную");
    await expect(page.getByRole("button", { name: "Скопировать" })).toBeVisible();
    await expect(page.getByText(/Форма на сайте вуза не позволяет/)).toBeVisible();
    // Ответ уже в истории, а карточка с текстом не пропала.
    await expect(composer(page)).toBeEnabled();
    await expect(page.getByRole("button", { name: "Скопировать" })).toBeVisible();
    await expect(page.getByRole("link", { name: /Открыть сайт вуза/ })).toHaveAttribute("target", "_blank");
  });

  test("нет кнопок «Повторить», «Обновить», «Загрузить» в чате", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "сбой");
    await expect(page.getByText(/лимит ответов исчерпан/)).toBeVisible();
    const labels = await page.getByRole("button").allTextContents();
    for (const label of labels)
      expect(label.trim()).not.toMatch(/^(Повторить|Обновить|Загрузить|Попробовать снова)$/);
  });
});

test.describe("нет сети", () => {
  test("сообщение ждёт с подписью и уходит само, когда сеть вернулась", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await expect(composer(page)).toBeVisible();
    // Браузер сообщает, что сети нет; мок отвечает локально, поэтому сеть «отключаем» признаком onLine.
    await page.evaluate(() => {
      Object.defineProperty(navigator, "onLine", { configurable: true, get: () => false });
      window.dispatchEvent(new Event("offline"));
    });
    await ask(page, "Что сдавать на неделе?");
    await expect(page.getByText("Отправится, когда появится сеть")).toBeVisible();
    await expect(timeline(page).getByText("Что сдавать на неделе?", { exact: true })).toBeVisible();
    await page.evaluate(() => {
      Object.defineProperty(navigator, "onLine", { configurable: true, get: () => true });
      window.dispatchEvent(new Event("online"));
    });
    await expect(page.getByText(/Ближайшие работы/)).toBeVisible();
    await expect(page.getByText("Отправится, когда появится сеть")).toHaveCount(0);
    await expect(timeline(page).getByText("Что сдавать на неделе?", { exact: true })).toHaveCount(1);
  });
});

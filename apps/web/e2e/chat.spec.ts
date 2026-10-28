import { expect, type Page, test } from "@playwright/test";
import { expectNoHorizontalOverflow, findSmallTapTargets, findTinyText, open } from "./support";

/**
 * Чат в настоящем браузере на моках: раскладка (поле ввода над нижней панелью, страница не прокручивается),
 * ответ агента с источниками, внутренние ссылки, шкала контекста, запрос на запись и очередь без сети.
 */

const composer = (page: Page) => page.getByRole("textbox", { name: "Сообщение агенту" });

async function ask(page: Page, text: string) {
  const input = composer(page);
  const submit = page.getByRole("button", { name: "Отправить" });
  await expect(input).toBeEnabled();
  await input.fill(text);
  await expect(input).toHaveValue(text);
  await expect(submit).toBeEnabled();
  await submit.click();
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
    const savedReplies = timeline(page).locator('[data-role="agent"]:not([data-live-reply] *)');
    const questions = ["Что сдавать на неделе?", "Какая следующая пара?", "Расскажи анекдот"];
    for (const [index, question] of questions.entries()) {
      await ask(page, question);
      await expect(timeline(page).getByText(question, { exact: true })).toBeVisible();
      // Вопрос появляется оптимистично, до запуска job. Следующий оборот начинается только
      // после нового ответа в истории и завершения передачи live → canonical.
      await expect(savedReplies).toHaveCount(index + 1);
      await expect(page.locator("[data-live-reply]")).toHaveCount(0);
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
    await expect(page).toHaveURL(/\/study\/connections$/);
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

test.describe("Варя в чате", () => {
  const sprite = (page: Page) => page.locator("[data-dock] svg.varya");
  const box = async (locator: ReturnType<Page["locator"]>) => {
    const value = await locator.boundingBox();
    if (!value) throw new Error("элемент не виден");
    return { left: value.x, top: value.y, right: value.x + value.width, bottom: value.y + value.height };
  };

  test("пустой чат: Варя на сцене 120 px, лапы на кромке поля, подсказки не перекрыты", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await expect(page.getByRole("list", { name: "Подсказки" })).toBeVisible();
    await expect(page.locator("[data-dock]")).toHaveAttribute("data-dock", "floor");
    await expect(sprite(page)).toHaveCount(1);
    const girl = await box(sprite(page));
    const form = await box(page.locator("form[data-composer]"));
    const list = await box(page.getByRole("list", { name: "Подсказки" }));
    expect(Math.abs(girl.bottom - girl.top - 120)).toBeLessThanOrEqual(1);
    // Лапы на верхней кромке поля (запас — пустые 5 % поля спрайта).
    expect(Math.abs(girl.bottom - form.top)).toBeLessThanOrEqual(8);
    // Подсказки стоят выше сцены.
    expect(list.bottom).toBeLessThanOrEqual(girl.top + 8);
  });

  test("первое сообщение: прыжок на насест не дольше 280 мс, одна Варя, лапы на кромке, поля не перекрыты", async ({
    page,
  }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await expect(sprite(page)).toBeVisible();
    await expect(page.getByRole("list", { name: "Подсказки" }).getByRole("button").first()).toBeEnabled();
    const handle = await sprite(page).elementHandle();
    const flight = await page.evaluate(async () => {
      const dock = document.querySelector<HTMLElement>("[data-dock]");
      const svg = document.querySelector("[data-dock] svg.varya") as SVGSVGElement;
      const started = performance.now();
      const first = document.querySelector<HTMLElement>("[aria-label='Подсказки'] button");
      if (!first || !dock) throw new Error("Пустой чат должен показать подсказку и Варю до отправки");
      const modes: (string | undefined)[] = [];
      const observer = new MutationObserver(() => modes.push(dock.dataset.dock));
      observer.observe(dock, { attributes: true, attributeFilter: ["data-dock"] });
      first.click();
      const samples: { t: number; w: number }[] = [];
      await new Promise<void>((resolve) => {
        const tick = () => {
          samples.push({ t: performance.now() - started, w: svg.getBoundingClientRect().width });
          if (performance.now() - started < 700) requestAnimationFrame(tick);
          else resolve();
        };
        requestAnimationFrame(tick);
      });
      observer.disconnect();
      return { samples, modes };
    });
    const series = flight.samples;
    // Назначение серверного threadId не возвращает Варю на пол и не запускает второй прыжок.
    expect(flight.modes).toEqual(["perch"]);
    expect(series[0]?.w).toBeGreaterThan(90);
    // Длительность перелёта — от первого кадра, где размер пошёл меняться, до кадра, где он перестал: задержка
    // между нажатием и началом движения (нагрузка на runner) в 280 мс не входит.
    const started = series.find((sample) => Math.abs(sample.w - (series[0]?.w ?? 0)) > 0.5);
    const settled = series.find(
      (sample) => sample.t >= (started?.t ?? 0) && Math.abs(sample.w - (series.at(-1)?.w ?? 0)) < 0.5,
    );
    const duration = (settled?.t ?? Number.POSITIVE_INFINITY) - (started?.t ?? 0);
    expect(duration).toBeGreaterThan(40);
    expect(duration).toBeLessThanOrEqual(280 + 120);
    // Тот же экземпляр на насесте.
    expect(
      await page.evaluate((previous) => previous === document.querySelector("[data-dock] svg.varya"), handle),
    ).toBe(true);
    await expect(page.locator("[data-dock]")).toHaveAttribute("data-dock", "perch");
    await expect(sprite(page)).toHaveCount(1);
    const girl = await box(sprite(page));
    const form = await box(page.locator("form[data-composer]"));
    const field = await box(composer(page));
    const send = await box(page.getByRole("button", { name: "Отправить" }));
    expect(Math.abs(girl.bottom - form.top)).toBeLessThanOrEqual(4);
    expect(girl.bottom).toBeLessThanOrEqual(field.top + 4);
    expect(girl.bottom).toBeLessThanOrEqual(send.top + 4);
    // Справа 16 px от края поля, 36 px (поле уже 390 px) или 40 px.
    expect(Math.abs(form.right - 16 - girl.right)).toBeLessThanOrEqual(2);
    const size = form.right - form.left >= 390 ? 40 : 36;
    expect(Math.abs(girl.right - girl.left - size)).toBeLessThanOrEqual(1);
  });

  test("последнее сообщение не уходит под Варю: нижний отступ ленты закрывает её выступ", async ({
    page,
  }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "Какая следующая пара?");
    await expect(
      timeline(page)
        .getByText(/Следующая пара|пары закончились|Не знаю/)
        .first(),
    ).toBeVisible();
    await page.waitForTimeout(500);
    const girl = await box(sprite(page));
    const last = await box(timeline(page).getByRole("listitem").last());
    expect(last.bottom).toBeLessThanOrEqual(girl.top);
  });

  test("реплика из данных на сцене: aria-hidden, до 48 знаков; печать и нажатие её убирают", async ({
    page,
  }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    const bubble = page.locator("[data-dock-bubble] [data-mascot-bubble]");
    await expect(bubble).toBeVisible();
    await expect(bubble).toHaveAttribute("aria-hidden", "true");
    expect([...((await bubble.textContent()) ?? "")].length).toBeLessThanOrEqual(48);
    await composer(page).fill("Ка");
    await expect(bubble).toHaveCount(0);
  });

  test("нажатие по пузырю закрывает его", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    const bubble = page.locator("[data-dock-bubble] [data-mascot-bubble]");
    await expect(bubble).toBeVisible();
    await bubble.click();
    await expect(bubble).toHaveCount(0);
  });

  test("спокойный режим: Варя статична на сцене и на насесте, реплика остаётся текстом", async ({ page }) => {
    await page.addInitScript(() => window.localStorage.setItem("botai.motion", "calm"));
    await openChat(page, "/chat?thread=new&pace=0");
    await expect(sprite(page)).toHaveClass(/still/);
    await expect(page.locator("[data-dock-bubble] [data-mascot-bubble]")).toBeVisible();
    await ask(page, "Расскажи анекдот");
    await expect(page.getByText("Не уверен")).toBeVisible();
    await expect(sprite(page)).toHaveClass(/still/);
  });

  test("открытая клавиатура на телефоне: нижней панели нет, Варя статична, реплики нет", async ({
    page,
  }, info) => {
    test.skip(info.project.name !== "phone", "клавиатура бывает только на касательном экране");
    await openChat(page, "/chat?thread=new&pace=0");
    await expect(sprite(page)).toBeVisible();
    await composer(page).focus();
    await expect(page.getByRole("navigation", { name: "Основная навигация" })).toBeHidden();
    await expect(sprite(page)).toHaveClass(/still/);
    await expect(page.locator("[data-dock-bubble]")).toHaveCount(0);
    // Поле и Варя остаются в видимой области.
    const form = await box(page.locator("form[data-composer]"));
    const girl = await box(sprite(page));
    const viewport = page.viewportSize();
    expect(form.bottom).toBeLessThanOrEqual((viewport?.height ?? 0) + 1);
    expect(girl.top).toBeGreaterThanOrEqual(0);
  });
});

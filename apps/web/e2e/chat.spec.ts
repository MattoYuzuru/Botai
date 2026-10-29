import { expect, type Locator, type Page, test } from "@playwright/test";
import { expectNoHorizontalOverflow, findSmallTapTargets, findTinyText, open } from "./support";

/**
 * Чат в настоящем браузере на моках: чат на весь экран (шапка с кнопками, лента прокручивается страницей, поле ввода
 * у низа), панели «Чаты» и «Источники», ответ агента с таблицами, графиком, планом и быстрыми ответами, вложения,
 * очередь без сети. Ноутбук — 1280×800, телефон — 390×844 (проекты Playwright).
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
const isLaptop = (name: string) => name === "laptop";

const box = async (locator: Locator) => {
  const value = await locator.boundingBox();
  if (!value) throw new Error("элемент не виден");
  return { left: value.x, top: value.y, right: value.x + value.width, bottom: value.y + value.height };
};

test.describe("раскладка чата на весь экран", () => {
  test("ноутбук: верхняя полоса во всю ширину области, название слева, кнопки справа на линии знака рейла", async ({
    page,
  }, info) => {
    test.skip(!isLaptop(info.project.name), "верхняя полоса с подписями — раскладка ноутбука");
    await openChat(page);
    const bar = page.locator("[data-chat-topbar]");
    await expect(bar).toBeVisible();
    const layout = await page.evaluate(() => {
      const rect = (selector: string) => {
        const element = document.querySelector(selector);
        if (!element) throw new Error(selector);
        const value = element.getBoundingClientRect();
        return { left: value.left, right: value.right, top: value.top, bottom: value.bottom };
      };
      const center = (value: { top: number; bottom: number }) => (value.top + value.bottom) / 2;
      const nav = rect("nav[aria-label='Основная навигация']");
      const bar = rect("[data-chat-topbar]");
      const title = rect("[data-chat-topbar] h1");
      const logo = rect("nav [role='img'][aria-label='BotAI']");
      const buttons = ["Источники", "Чаты", "Новый чат"].map((name) => {
        const button = [...document.querySelectorAll<HTMLElement>("[data-chat-topbar] button")].find(
          (item) => item.textContent?.trim() === name,
        );
        if (!button) throw new Error(name);
        const value = button.getBoundingClientRect();
        return { name, left: value.left, right: value.right, center: center(value), text: true };
      });
      return {
        navRight: nav.right,
        bar,
        title,
        logoCenter: center(logo),
        buttons,
        clientWidth: document.documentElement.clientWidth,
        caption: document.querySelector("[data-chat-topbar] p")?.textContent,
      };
    });
    // Полоса занимает всю область справа от рейла.
    expect(Math.abs(layout.bar.left - layout.navRight)).toBeLessThanOrEqual(1);
    expect(Math.abs(layout.bar.right - layout.clientWidth)).toBeLessThanOrEqual(1);
    // Название — слева, подпись «Чат» под ним, кнопки — справа.
    expect(layout.title.left - layout.bar.left).toBeLessThanOrEqual(40);
    expect(layout.caption).toBe("Чат");
    for (const button of layout.buttons) {
      expect(button.left).toBeGreaterThan(layout.bar.left + (layout.bar.right - layout.bar.left) / 2);
      // На одной линии со знаком BotAI в рейле.
      expect(
        Math.abs(button.center - layout.logoCenter),
        `${button.name}: линия со знаком рейла`,
      ).toBeLessThanOrEqual(2);
    }
    expect(layout.buttons.map((button) => button.name)).toEqual(["Источники", "Чаты", "Новый чат"]);
    // Последняя кнопка у правого края полосы, как «Поделиться» и меню в ChatGPT.
    expect(layout.bar.right - (layout.buttons.at(-1)?.right ?? 0)).toBeLessThanOrEqual(32);
  });

  test("ноутбук: поле ввода прижато к низу с отступом 16–24 px, колонка 48rem по центру области", async ({
    page,
  }, info) => {
    test.skip(!isLaptop(info.project.name), "отступ от низа и колонка — раскладка ноутбука");
    await openChat(page);
    await expect(composer(page)).toBeVisible();
    const geometry = await page.evaluate(() => {
      const form = document.querySelector("form[data-composer]")?.getBoundingClientRect();
      const nav = document.querySelector("nav[aria-label='Основная навигация']")?.getBoundingClientRect();
      if (!form || !nav) throw new Error("нет поля ввода");
      return {
        bottomGap: window.innerHeight - form.bottom,
        width: form.width,
        center: form.left + form.width / 2,
        areaCenter: nav.right + (document.documentElement.clientWidth - nav.right) / 2,
        fontSize: Number.parseFloat(getComputedStyle(document.querySelector("textarea") as Element).fontSize),
      };
    });
    expect(geometry.bottomGap).toBeGreaterThanOrEqual(16);
    expect(geometry.bottomGap).toBeLessThanOrEqual(24);
    expect(geometry.width).toBeLessThanOrEqual(768);
    expect(geometry.width).toBeGreaterThan(700);
    expect(Math.abs(geometry.center - geometry.areaCenter)).toBeLessThanOrEqual(2);
    expect(geometry.fontSize).toBeGreaterThanOrEqual(16);
  });

  test("страница прокручивается целиком: скроллбар у края окна, внутри ленты своей прокрутки нет", async ({
    page,
  }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    for (const question of ["Какая следующая пара?", "Покажи график", "Сравни нагрузку и составь план"]) {
      await ask(page, question);
      await expect(timeline(page).getByText(question, { exact: true })).toBeVisible();
      await expect(composer(page)).toBeEnabled();
    }
    await expect(page.getByText(/выполнено 3 из 3/)).toBeVisible();
    await page.waitForTimeout(300);
    const state = await page.evaluate(() => {
      const root = document.scrollingElement as HTMLElement;
      const layout = document.querySelector("[data-chat-layout]") as HTMLElement;
      // Ни один контейнер внутри экрана чата не прокручивается сам (кроме поля ввода).
      const nested = [...layout.querySelectorAll<HTMLElement>("*")]
        // Широкая таблица, график и код листаются по горизонтали сами — это не лента.
        .filter((element) => !element.closest("textarea, [data-md-table], [data-chart], pre"))
        .filter((element) => /auto|scroll/.test(getComputedStyle(element).overflowY))
        .map((element) => element.tagName + (element.getAttribute("data-radix-scroll-area-viewport") ?? ""));
      const header = document.querySelector("[data-chat-header]")?.getBoundingClientRect();
      const dock = document.querySelector("[data-chat-dock]")?.getBoundingClientRect();
      return {
        scrollable: root.scrollHeight - root.clientHeight,
        atBottom: root.scrollHeight - root.scrollTop - root.clientHeight,
        scrollTop: root.scrollTop,
        nested,
        viewport: document.querySelector("[data-radix-scroll-area-viewport]") !== null,
        headerTop: header?.top,
        dockBottom: dock?.bottom,
        innerHeight: window.innerHeight,
        scrollbarWidth: getComputedStyle(document.documentElement).scrollbarWidth,
        layoutRight: layout.getBoundingClientRect().right,
        clientWidth: document.documentElement.clientWidth,
      };
    });
    // Длинная переписка выходит за экран, а прокручивает её страница, и лента держится у последнего сообщения.
    expect(state.scrollable).toBeGreaterThan(200);
    expect(state.atBottom).toBeLessThanOrEqual(4);
    expect(state.nested).toEqual([]);
    expect(state.viewport).toBe(false);
    // Шапка липнет к верху, поле — к низу, скроллбар у страницы тонкий.
    expect(state.headerTop).toBeLessThanOrEqual(1);
    expect(state.dockBottom).toBeLessThanOrEqual(state.innerHeight + 1);
    expect(state.scrollbarWidth).toBe("thin");
    // Рамка экрана — вся ширина окна: полоса прокрутки принадлежит окну, а не колонке.
    expect(Math.abs(state.layoutRight - state.clientWidth)).toBeLessThanOrEqual(1);
    // Прокрутка вверх работает страницей, а шапка остаётся на месте.
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect
      .poll(() => page.evaluate(() => (document.scrollingElement as HTMLElement).scrollTop))
      .toBe(0);
    expect((await box(page.locator("[data-chat-header]"))).top).toBeLessThanOrEqual(1);
    await expect(page.getByRole("button", { name: "К последнему сообщению" })).toBeVisible();
    await page.getByRole("button", { name: "К последнему сообщению" }).click();
    await expect
      .poll(() =>
        page.evaluate(() => {
          const root = document.scrollingElement as HTMLElement;
          return root.scrollHeight - root.scrollTop - root.clientHeight;
        }),
      )
      .toBeLessThanOrEqual(4);
  });

  test("телефон: компактная шапка с иконками, поле ввода над нижней панелью с safe area", async ({
    page,
  }, info) => {
    test.skip(isLaptop(info.project.name), "компактная шапка — раскладка телефона");
    await openChat(page);
    await expect(composer(page)).toBeVisible();
    const layout = await page.evaluate(() => {
      const rect = (selector: string) => {
        const element = document.querySelector(selector);
        if (!element) throw new Error(selector);
        return element.getBoundingClientRect();
      };
      const nav = rect("nav[aria-label='Основная навигация']");
      const form = rect("form[data-composer]");
      const bar = rect("[data-chat-topbar]");
      const labels = [...document.querySelectorAll<HTMLElement>("[data-chat-topbar] button")].map(
        (button) => button.textContent?.trim() ?? "",
      );
      return {
        formBottom: form.bottom,
        navTop: nav.top,
        navHeight: nav.height,
        barHeight: bar.height,
        labels,
        innerHeight: window.innerHeight,
      };
    });
    expect(layout.navHeight).toBeGreaterThan(40);
    expect(layout.formBottom).toBeLessThanOrEqual(layout.navTop + 1);
    // Иконки без текстовых подписей: полоса компактная, но зоны нажатия ≥ 44 px проверяет findSmallTapTargets.
    expect(layout.labels.filter((label) => label !== "" && !/^\d/.test(label))).toEqual([]);
    expect(layout.barHeight).toBeLessThanOrEqual(64);
    for (const name of ["Источники", "Чаты", "Новый чат"]) {
      await expect(page.locator("[data-chat-topbar]").getByRole("button", { name })).toBeVisible();
    }
    expect(await findSmallTapTargets(page)).toEqual([]);
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
});

test.describe("панели «Чаты» и «Источники»", () => {
  test("«Чаты»: список, «Новый чат» открывает пустой чат", async ({ page }) => {
    await openChat(page);
    await page.getByRole("button", { name: "Чаты" }).click();
    const sheet = page.getByRole("dialog", { name: "Чаты" });
    await expect(sheet.getByRole("link", { name: /Что сегодня по расписанию\?/ })).toBeVisible();
    await sheet.getByRole("button", { name: "Новый чат" }).click();
    await expect(page).toHaveURL(/thread=new/);
    await expect(page.getByRole("heading", { level: 2, name: "Спросите про пары и сроки" })).toBeVisible();
  });

  test("«Источники»: вузы и источники со статусами; «Добавить источник» кладёт заготовку в поле нового чата", async ({
    page,
  }) => {
    await openChat(page);
    await page.getByRole("button", { name: "Источники" }).click();
    const sheet = page.getByRole("dialog", { name: "Источники" });
    await expect(sheet.getByRole("heading", { name: "Высшая школа экономики" })).toBeVisible();
    await expect(sheet.getByRole("heading", { name: "Центральный университет" })).toBeVisible();
    await expect(sheet.getByText("Работает").first()).toBeVisible();
    await sheet.getByRole("link", { name: "Добавить источник для ВШЭ" }).click();
    await expect(page).toHaveURL(/thread=new/);
    await expect(composer(page)).toHaveValue("Добавь источник для ВШЭ: ");
    // Заготовка не отправлена сама.
    await expect(page.locator("[data-timeline]")).toHaveCount(0);
  });

  test("/chat?draft=… открывает новый чат с заполненным полем и не отправляет его", async ({ page }) => {
    await openChat(page, `/chat?draft=${encodeURIComponent("Хочу подключить вуз")}&pace=0`);
    await expect(composer(page)).toHaveValue("Хочу подключить вуз");
    await expect(page).toHaveURL(/thread=new/);
    await expect(page.locator("[data-timeline]")).toHaveCount(0);
  });

  test("подключение идёт в треде: после ухода в «Чаты» у него «Ждёт вас»", async ({ page }) => {
    await open(page, "/chat?thread=new&pace=0", "empty");
    await ask(page, "Подключи ЦУ https://new.fakeuni.test");
    await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
    await page.getByRole("button", { name: "Новый чат" }).click();
    await page.getByRole("button", { name: "Чаты" }).click();
    const sheet = page.getByRole("dialog", { name: "Чаты" });
    const row = sheet.getByRole("listitem").first();
    await expect(row.getByText("Ждёт вас")).toBeVisible();
    await row.getByRole("link").click();
    await expect(page.getByText("Вход в lk.fakeuni.test. Логин")).toBeVisible();
  });

  test("агент просит войти вне открытого треда: тост со ссылкой ведёт прямо в него, в списке — «Ждёт вас» и кружок", async ({
    page,
  }, info) => {
    test.skip(!isLaptop(info.project.name), "ожидание опроса списка чатов — один раз, на ноутбуке");
    test.setTimeout(75_000);
    // Сценарий входа идёт в реальном темпе: пока человек в другом чате, тред сначала «работает», потом «ждёт вас».
    await open(page, "/chat?thread=new", "empty");
    await ask(page, "Подключи ЦУ https://new.fakeuni.test");
    await expect(page.getByText(/Запустил подключение/)).toBeVisible();
    await page.getByRole("button", { name: "Новый чат" }).click();
    await expect(page.getByRole("heading", { level: 2, name: "Спросите про пары и сроки" })).toBeVisible();
    const toast = page.getByText(/Ботай просит войти: Подключи ЦУ/);
    await expect(toast).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "Чаты" }).click();
    const sheet = page.getByRole("dialog", { name: "Чаты" });
    await expect(sheet.getByText("Ждёт вас")).toBeVisible();
    await expect(sheet.getByRole("img", { name: "Есть непрочитанное" })).toBeVisible();
    await sheet.getByRole("button", { name: "Закрыть" }).click();
    await page.getByRole("button", { name: "Открыть", exact: true }).click();
    await expect(page.getByText(/Вход в lk\.fakeuni\.test\. Логин/)).toBeVisible();
  });
});

test.describe("ответ агента", () => {
  test("история, цитаты одной записью по источнику и внутренняя ссылка: подключение открывается в «Учёбе»", async ({
    page,
  }) => {
    await openChat(page);
    await expect(timeline(page).getByText("Что сегодня по расписанию?", { exact: true })).toBeVisible();
    const sources = page.getByRole("list", { name: "Источники" });
    await expect(sources.getByRole("listitem")).toHaveCount(1);
    // Название источника, а не «lk.fakeuni.test» подряд.
    await expect(sources.getByText("lk.fakeuni.test", { exact: true })).toHaveCount(0);
    await sources.getByRole("button", { name: /Личный кабинет/ }).click();
    await expect(sources.getByRole("link", { name: /Расписание на сайте вуза/ })).toHaveAttribute(
      "target",
      "_blank",
    );
    await sources.getByRole("link", { name: /Открыть подключение в «Учёбе»/ }).click();
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
    const segments = await sheet.locator("[data-context-bar] [data-segment]").count();
    expect(segments).toBeGreaterThanOrEqual(3);
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
  });

  test("таблица с выделением, курсив, ссылка, быстрые ответы: нажатие отправляет текст", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "Какая следующая пара?");
    const table = page.getByRole("table");
    await expect(table).toBeVisible();
    await expect(table.getByRole("columnheader")).toHaveText(["Время", "Предмет", "Аудитория"]);
    await expect(page.locator("em", { hasText: "Расписание обновлено" })).toBeVisible();
    await expect(page.getByRole("link", { name: "в кабинете" })).toHaveAttribute("target", "_blank");
    const replies = page.getByRole("group", { name: "Быстрые ответы" });
    await expect(replies).toBeVisible();
    await replies.getByRole("button", { name: "Что сдавать на неделе?" }).click();
    await expect(page.getByText(/Ближайшие работы/)).toBeVisible();
  });

  test("график и план ответа: доступная картинка, таблица-альтернатива, шаги по состояниям", async ({
    page,
  }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "Покажи график");
    const figure = page.getByRole("figure");
    await expect(figure).toBeVisible();
    await expect(figure.getByRole("img")).toHaveAccessibleName(/Пар по дням/);
    await figure.getByRole("button", { name: "Показать таблицу" }).click();
    await expect(figure.getByRole("table")).toBeVisible();
    await expect(composer(page)).toBeEnabled();
    await ask(page, "Сравни нагрузку и составь план");
    await expect(page.getByText(/выполнено 3 из 3/)).toBeVisible();
    await expect(page.getByText(/^План ·/)).toHaveCount(1);
  });

  test("подсказка отправляет вопрос; ответ с источником и без «Не уверен»", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await page.getByRole("list", { name: "Подсказки" }).getByRole("button").first().click();
    await expect(page.getByRole("list", { name: "Источники" })).toBeVisible();
    await expect(page.getByText("Не уверен")).toHaveCount(0);
  });

  test("«Не знаю» помечено «Не уверен»", async ({ page }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "Расскажи анекдот");
    await expect(page.getByText("Не уверен")).toBeVisible();
    await expect(page.getByText(/^Не знаю:/)).toBeVisible();
  });

  test("запрос на запись: вопрос у поля ввода, карточка с методом, хостом и полями, одобрение", async ({
    page,
  }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await ask(page, "Мне нужна справка");
    await expect(page.getByText("Какая справка нужна?")).toBeVisible();
    await expect(composer(page)).toBeDisabled();
    await expect(composer(page)).toHaveAttribute("placeholder", "Ответьте на вопрос выше");
    // Открытый вопрос стоит у поля ввода: он и поле на одном экране.
    const question = await box(page.locator("[data-pending-questions]"));
    const field = await box(composer(page));
    expect(field.top - question.bottom).toBeLessThan(120);
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

test.describe("вложения", () => {
  const png = {
    name: "доска.png",
    mimeType: "image/png",
    // 1×1 прозрачный PNG.
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
      "base64",
    ),
  };

  test("файл грузится сразу, чип с удалением; отправка без текста; фото видно в сообщении и открывается во вьюере", async ({
    page,
  }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await page.locator("[data-composer-file]").setInputFiles(png);
    const chips = page.getByRole("list", { name: "Вложения" });
    await expect(chips.getByRole("button", { name: "Открыть доска.png" })).toBeEnabled();
    await expect(chips.getByRole("button", { name: "Убрать доска.png" })).toBeVisible();
    await page.getByRole("button", { name: "Отправить" }).click();
    const thumb = timeline(page).getByRole("img", { name: "доска.png" });
    await expect(thumb).toBeVisible();
    await expect(page.getByText(/Изображение «доска\.png» получено/)).toBeVisible();
    await expect(chips).toHaveCount(0);
    await timeline(page).getByRole("button", { name: "Открыть доска.png" }).click();
    const viewer = page.getByRole("dialog");
    await expect(viewer.getByRole("img", { name: "доска.png" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(viewer).toBeHidden();
  });

  test("чип убирается до отправки; перетаскивание файла на область чата добавляет вложение", async ({
    page,
  }) => {
    await openChat(page, "/chat?thread=new&pace=0");
    await page.locator("[data-composer-file]").setInputFiles(png);
    const chips = page.getByRole("list", { name: "Вложения" });
    await expect(chips.getByRole("button", { name: "Открыть доска.png" })).toBeEnabled();
    await chips.getByRole("button", { name: "Убрать доска.png" }).click();
    await expect(chips).toHaveCount(0);

    const data = await page.evaluateHandle(
      ({ name, type }) => {
        const transfer = new DataTransfer();
        transfer.items.add(new File(["лекция"], name, { type }));
        return transfer;
      },
      { name: "лекция.pdf", type: "application/pdf" },
    );
    await page.locator("[data-drop-zone]").dispatchEvent("dragenter", { dataTransfer: data });
    await expect(page.getByText("Отпустите файлы, чтобы прикрепить")).toBeVisible();
    await page.locator("[data-drop-zone]").dispatchEvent("drop", { dataTransfer: data });
    await expect(page.getByRole("list", { name: "Вложения" }).getByText("лекция.pdf")).toBeVisible();
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

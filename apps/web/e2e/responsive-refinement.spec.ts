import { expect, type Page, test } from "@playwright/test";
import { expectFitsWidth, freezeClock, open } from "./data-support";
import { expectSheetReady } from "./support";

test.beforeEach(async ({ page }) => freezeClock(page));

test("шапка, предупреждение и пустые данные на телефоне и большом экране", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  const width = phone ? 400 : 1920;
  await page.setViewportSize({ width, height: phone ? 775 : 1080 });
  await open(page, "/", "connected");
  const stored = await page.evaluate(async () => {
    const path = "/src/mocks/db.ts";
    const { createDb, persistDb } = await import(/* @vite-ignore */ path);
    const db = createDb("connected");
    db.spaces = db.spaces.slice(0, 1);
    db.connections = db.connections
      .filter((row: { spaceId: string }) => row.spaceId === db.spaces[0].id)
      .slice(0, 1);
    Object.assign(db.connections[0], {
      status: "error",
      errorCode: "login_failed",
      errorDetail:
        "Вуз не принял данные три раза подряд. Мы остановились, чтобы не заблокировать аккаунт: попробуйте через 10 мин.",
    });
    persistDb(db);
  });
  await page.goto("/?data=quiet&latency=0");
  const title = page.getByRole("heading", { name: "Сегодня", exact: true });
  await expect(title).toBeVisible();
  const notice = page.getByRole("list", { name: "Состояние источников" });
  await expect(notice.getByText(/Вуз отклонил вход/)).toBeVisible();
  const action = notice.getByRole("link", { name: "Спросить в чате" });
  const text = notice.locator("[data-source-state]");
  const a = await action.boundingBox();
  const t = await text.boundingBox();
  expect(a && t).toBeTruthy();
  if (!a || !t) return;
  if (phone) {
    expect(a.y).toBeGreaterThanOrEqual(t.y + t.height);
    expect(t.width).toBeGreaterThan(250);
  } else {
    expect(Math.abs(a.y + a.height / 2 - t.y - t.height / 2)).toBeLessThan(2);
    const brand = await page.getByRole("img", { name: "BotAI", exact: true }).boundingBox();
    expect(Math.abs(((await title.boundingBox())?.y ?? 0) - (brand?.y ?? 0))).toBeLessThan(8);
    const empty = await page.getByText("Пар в этих данных нет", { exact: true }).boundingBox();
    expect(empty?.y ?? 0).toBeGreaterThan(400);
  }
  await expectFitsWidth(page, width);
  await page.screenshot({ path: info.outputPath(`today-${width}.png`), fullPage: true });
});

/** Три вуза в базе мока: ВШЭ, ЦУ и «Синтетический университет»; «Сегодня» без данных, подключения устарели. */
async function openWithThreeSpaces(page: Page) {
  await open(page, "/?data=quiet", "stale");
  const stored = await page.evaluate(async () => {
    const path = "/src/mocks/db.ts";
    const { createDb, persistDb, ulid } = await import(/* @vite-ignore */ path);
    const db = createDb("stale");
    const third = {
      ...db.spaces[0],
      id: ulid(),
      title: "Синтетический университет",
      shortTitle: "СУ",
      universityId: null,
    };
    db.spaces.push(third);
    db.connections.push({ ...db.connections[0], id: ulid(), spaceId: third.id });
    persistDb(db);
    return window.sessionStorage.getItem("botai.mock.db");
  });
  // Фоновый запрос открытой страницы (опрос чатов, прочтение) может сохранить свою копию базы после нашей:
  // кладём базу до запуска скриптов следующей загрузки, а не полагаемся на порядок записей.
  await page.addInitScript((value) => {
    if (value) window.sessionStorage.setItem("botai.mock.db", value);
  }, stored);
  await page.goto("/?data=quiet&latency=0");
}

test("три вуза в шапке: пояснение раскрывается плавно, соседи сдвигаются, без фильтрации", async ({
  page,
}, info) => {
  const phone = info.project.name === "phone";
  await page.setViewportSize({ width: phone ? 400 : 1512, height: phone ? 775 : 982 });
  await openWithThreeSpaces(page);
  const statuses = page.locator("header").getByRole("list", { name: "Статусы вузов" });
  const chips = statuses.getByRole("button");
  await expect(chips).toHaveCount(3);
  const status = chips.first();
  const detail = status.locator("[data-source-detail]");
  const label = detail.locator("span.block");
  await expect(status).toHaveAttribute("aria-pressed", "false");
  expect((await detail.boundingBox())?.width).toBe(0);

  // Плавность проверяем по свойствам перехода, а не по таймингам: ширина идёт через grid-template-columns
  // на токенах движения, max-width не анимируется, чипы стоят в одной строке и не переносятся.
  const motion = await detail.evaluate((node) => {
    const style = getComputedStyle(node);
    return {
      property: style.transitionProperty,
      duration: style.transitionDuration,
      easing: style.transitionTimingFunction,
      columns: style.gridTemplateColumns,
    };
  });
  expect(motion.property).toContain("grid-template-columns");
  expect(motion.property).not.toContain("max-width");
  expect(motion.duration).toContain("0.28s");
  expect(motion.easing).toContain("cubic-bezier(0.22, 1, 0.36, 1)");
  expect(motion.columns).toBe("0px");
  const tops = await chips.evaluateAll((nodes) =>
    nodes.map((node) => Math.round(node.getBoundingClientRect().top)),
  );
  expect(new Set(tops).size, "чипы в одной строке").toBe(1);

  const lefts = () =>
    chips.evaluateAll((nodes) => nodes.map((node) => Math.round(node.getBoundingClientRect().left)));
  const rest = await lefts();
  await status.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(label).toBeVisible();
  // Раскрытый чип стоит на месте, соседи уехали вправо; при потере фокуса вернулись тем же переходом.
  await expect.poll(async () => (await lefts())[1]).toBeGreaterThan(rest[1] ?? 0);
  expect((await lefts())[0]).toBe(rest[0]);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await expect.poll(lefts).toEqual(rest);

  await status.click();
  await expect(status).toHaveAttribute("aria-pressed", "true");
  await expect(detail).toHaveCSS("opacity", "1");
  await expect(statuses.getByRole("button", { pressed: true })).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(status).toHaveAttribute("aria-pressed", "false");
  await status.click();
  await page.getByRole("heading", { name: "Сегодня", exact: true }).click();
  await expect(status).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator("[data-screen=today]")).toHaveAttribute("data-state", "stale");
  await expectFitsWidth(page, phone ? 400 : 1512);

  await status.click();
  await expect(label).toBeVisible();
  await page.screenshot({ path: info.outputPath("freshness-expanded.png"), fullPage: true });

  // Сокращённое движение: переход не 0.28 с, а 1 мс, информация та же.
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await detail.evaluate((node) => getComputedStyle(node).transitionDuration)).toContain("0.001s");
});

test("320 px: раскрытый чип с длинным пояснением не выходит за экран и не выталкивает соседей", async ({
  page,
  context,
}, info) => {
  test.skip(info.project.name !== "phone", "узкий экран телефона");
  await page.setViewportSize({ width: 320, height: 700 });
  await openWithThreeSpaces(page);
  const chips = page.locator("header").getByRole("list", { name: "Статусы вузов" }).getByRole("button");
  await expect(chips).toHaveCount(3);
  // Без сети пояснение самое длинное: «Нет сети. Показан снимок от …».
  await context.setOffline(true);
  await expect(chips.first()).toHaveAttribute("data-source-state", "offline");
  for (const index of [0, 1, 2]) {
    await chips.nth(index).click();
    await expect(chips.nth(index)).toHaveAttribute("aria-pressed", "true");
    await expect
      .poll(
        () =>
          chips.evaluateAll((nodes) =>
            nodes.every((node) => {
              const box = node.getBoundingClientRect();
              return box.left >= 0 && box.right <= 320 && box.width >= 44;
            }),
          ),
        { message: `чипы внутри экрана при раскрытом чипе ${index}` },
      )
      .toBe(true);
    await expectFitsWidth(page, 320, `раскрыт чип ${index}`);
  }
});

test("laptop: наведение раскрывает пояснение, уход курсора возвращает; соседние чипы не перепрыгивают", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "laptop", "наведение мыши");
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, "/schedule", "connected");
  await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "ok");
  const chips = page.locator("header").getByRole("list", { name: "Статусы вузов" }).getByRole("button");
  const lefts = () =>
    chips.evaluateAll((nodes) => nodes.map((node) => Math.round(node.getBoundingClientRect().left)));
  const rest = await lefts();
  await chips.first().hover();
  await expect(chips.first().locator("[data-source-detail] span.block")).toBeVisible();
  await expect.poll(async () => (await lefts())[1]).toBeGreaterThan(rest[1] ?? 0);
  await page.mouse.move(640, 500);
  await expect.poll(lefts).toEqual(rest);
  await expect(chips.first()).toHaveAttribute("aria-pressed", "false");
});

test("«Источники» в чате: панель вузов, обе темы и крупный текст не переполняют экран", async ({
  page,
}, info) => {
  const width = info.project.name === "phone" ? 400 : 1512;
  await page.setViewportSize({ width, height: 982 });
  await open(page, "/chat?pace=0", "connected");
  await page.getByRole("button", { name: "Источники" }).click();
  await expectSheetReady(page, "Источники");
  const sheet = page.getByRole("dialog", { name: "Источники" });
  await expect(sheet.getByRole("heading", { name: "Высшая школа экономики" })).toBeVisible();
  await expect(sheet.getByRole("link", { name: "Добавить вуз" })).toBeVisible();
  for (const theme of ["light", "dark"]) {
    await page.evaluate((value) => {
      document.documentElement.dataset.theme = value;
    }, theme);
    await expectFitsWidth(page, width);
    await page.screenshot({ path: info.outputPath(`sources-${theme}.png`), fullPage: true });
  }
  await page.evaluate(() => document.documentElement.style.setProperty("font-size", "200%", "important"));
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth), {
      message: "ширина после перераскладки крупного текста",
    })
    .toBeLessThanOrEqual(width);
  await expect(sheet.getByRole("heading", { name: "Высшая школа экономики" })).toBeVisible();
  await page.screenshot({ path: info.outputPath("sources-200.png"), fullPage: true });
});

test("навигация уезжает и возвращается плавно; сокращённое движение отключает переход", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "phone", "экранная клавиатура на телефоне");
  await page.setViewportSize({ width: 400, height: 775 });
  await open(page, "/chat?thread=new&pace=0", "connected");
  const nav = page.getByRole("navigation", { name: "Основная навигация" });
  const input = page.getByRole("textbox", { name: "Сообщение агенту" });
  await expect(nav).toBeVisible();
  await input.focus();
  await expect(nav).toHaveAttribute("inert");
  const duration = await nav.evaluate((node) => getComputedStyle(node).transitionDuration);
  expect(duration).toContain("0.28s");
  await expect(nav).toBeHidden();
  await input.evaluate((node) => node.blur());
  await expect(nav).toBeVisible();
  await expect(nav).not.toHaveAttribute("inert");
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await nav.evaluate((node) => getComputedStyle(node).transitionDuration)).toContain("0.001s");
});

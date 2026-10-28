import { expect, test } from "@playwright/test";
import { expectFitsWidth, freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

test("шапка, предупреждение и пустые данные на телефоне и большом экране", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  const width = phone ? 400 : 1920;
  await page.setViewportSize({ width, height: phone ? 775 : 1080 });
  await open(page, "/", "connected");
  await page.evaluate(async () => {
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

test("три вуза в шапке: пояснение по фокусу и касанию без фильтрации", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  await page.setViewportSize({ width: phone ? 400 : 1512, height: phone ? 775 : 982 });
  await open(page, "/?data=quiet", "stale");
  await page.evaluate(async () => {
    const path = "/src/mocks/db.ts";
    const { createDb, persistDb, ulid } = await import(/* @vite-ignore */ path);
    const db = createDb("stale");
    const third = { ...db.spaces[0], id: ulid(), title: "Учебный вуз", universityId: null };
    db.spaces.push(third);
    db.connections.push({ ...db.connections[0], id: ulid(), spaceId: third.id });
    persistDb(db);
  });
  await page.goto("/?data=quiet&latency=0");
  const statuses = page.locator("header").getByRole("list", { name: "Свежесть данных" });
  await expect(statuses.getByRole("button")).toHaveCount(3);
  const status = statuses.getByRole("button").first();
  const label = status.locator("span.block");
  await expect(status).toHaveAttribute("aria-expanded", "false");
  expect((await label.boundingBox())?.width).toBe(0);
  await status.focus();
  await expect(label).toBeVisible();
  await status.click();
  await expect(status).toHaveAttribute("aria-expanded", "true");
  await expect(label.locator("..")).toHaveCSS("opacity", "1");
  await page.getByRole("heading", { name: "Сегодня", exact: true }).click();
  await expect(label).toBeVisible();
  await expect(page.locator("[data-screen=today]")).toHaveAttribute("data-state", "stale");
  await expectFitsWidth(page, phone ? 400 : 1512);
  await page.screenshot({ path: info.outputPath("freshness-expanded.png"), fullPage: true });
});

test("подключение: поиск с клавиатуры, отдельный повторный вход, обе темы и крупный текст", async ({
  page,
}, info) => {
  const width = info.project.name === "phone" ? 400 : 1512;
  await page.setViewportSize({ width, height: 982 });
  await open(page, "/connect", "connected");
  await expect(page.getByRole("heading", { name: "Ваши подключения" })).toBeVisible();
  const input = page.getByRole("combobox", { name: "Вуз" });
  await input.fill("ВШЭ");
  await expect(page.getByRole("option", { name: /Высшая школа/ })).toBeVisible();
  await input.press("ArrowDown");
  await input.press("Enter");
  await expect(page.getByText(/Этот вуз уже добавлен/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Начать подключение" })).toBeDisabled();
  for (const theme of ["light", "dark"]) {
    await page.evaluate((value) => {
      document.documentElement.dataset.theme = value;
    }, theme);
    const background = await input.evaluate(
      (node, value) =>
        getComputedStyle(node)
          .getPropertyValue(value === "dark" ? "--palette-slate-950" : "--palette-paper-100")
          .trim(),
      theme,
    );
    await expect(input.locator("..")).toHaveCSS("background-color", background);
    await expectFitsWidth(page, width);
    await page.screenshot({ path: info.outputPath(`connect-${theme}.png`), fullPage: true });
  }
  await page.evaluate(() => document.documentElement.style.setProperty("font-size", "200%", "important"));
  await expectFitsWidth(page, width);
  await expect(input).toBeVisible();
  await page.screenshot({ path: info.outputPath("connect-200.png"), fullPage: true });
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

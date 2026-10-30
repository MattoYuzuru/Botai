import { expect, type Page, test } from "@playwright/test";
import { open } from "./support";

/**
 * Диктовка в настоящем Chromium с поддельным микрофоном (`--use-fake-device-for-media-stream`: он звучит
 * периодическим гудком). Распознаёт стенд на моках: на любую запись он отвечает одной и той же фразой.
 */
test.use({
  permissions: ["microphone"],
  launchOptions: { args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] },
});

const PHRASE = "Какие у меня пары завтра";
const mic = (page: Page) => page.getByRole("button", { name: "Надиктовать текст" });
const field = (page: Page) => page.getByRole("textbox", { name: "Сообщение агенту" });

/** Самая большая высота столбика (0…1) за указанное время: читает то, что реально нарисовано. */
const tallestBar = (page: Page, ms: number) =>
  page.evaluate(
    (duration) =>
      new Promise<number>((resolve) => {
        const bars = Array.from(document.querySelectorAll<HTMLElement>("[data-bar]"));
        let best = 0;
        const started = performance.now();
        const look = () => {
          for (const bar of bars) {
            const value = Number(/scaleY\(([\d.]+)\)/.exec(bar.style.transform)?.[1] ?? 0);
            if (value > best) best = value;
          }
          if (performance.now() - started < duration) requestAnimationFrame(look);
          else resolve(best);
        };
        look();
      }),
    ms,
  );

/** Дождаться, пока поддельный микрофон гудит и столбики это показали: тихая запись на сервер не уходит. */
const untilHeard = (page: Page) =>
  expect.poll(() => tallestBar(page, 400), { timeout: 8000 }).toBeGreaterThan(0.2);

test("запись идёт по-настоящему: эквалайзер из столбиков по центру реагирует на звук, таймер идёт", async ({
  page,
}) => {
  await open(page, "/chat?thread=new&pace=0", "connected");
  await mic(page).click();
  const equalizer = page.locator("[data-equalizer='bars']");
  await expect(equalizer).toBeVisible();
  await expect(page.getByRole("button", { name: "Готово" })).toBeFocused();
  const count = await page.locator("[data-bar]").count();
  expect(count).toBeGreaterThanOrEqual(12);
  expect(count).toBeLessThanOrEqual(32);
  // Надписи «Идёт запись» на экране нет: её заменили столбики и время.
  await expect(page.locator("[data-recording-time]")).toHaveText("0:00");
  await expect(page.locator("[data-recording-time]")).toHaveText(/0:0[1-9]/);

  // Столбики отцентрованы по горизонтали и растут от средней линии: центры всех на одной высоте.
  const centres = await page.locator("[data-bar]").evaluateAll((bars) =>
    bars.map((bar) => {
      const box = bar.getBoundingClientRect();
      return Math.round(box.top + box.height / 2);
    }),
  );
  expect(new Set(centres).size).toBe(1);
  const box = await equalizer.boundingBox();
  const first = await page.locator("[data-bar]").first().boundingBox();
  const last = await page.locator("[data-bar]").last().boundingBox();
  if (!(box && first && last)) throw new Error("эквалайзер не виден");
  const left = first.x - box.x;
  const right = box.x + box.width - (last.x + last.width);
  expect(Math.abs(left - right)).toBeLessThanOrEqual(4);

  // Гудок поддельного микрофона поднимает хотя бы часть столбиков.
  expect(await tallestBar(page, 2600)).toBeGreaterThan(0.35);

  await page.getByRole("button", { name: "Отменить запись" }).click();
  await expect(field(page)).toBeFocused();
});

test("«Готово»: речь распознаётся, текст встаёт в поле с фокусом, сообщение не отправляется само", async ({
  page,
}) => {
  await open(page, "/chat?thread=new&pace=0", "connected");
  await mic(page).click();
  await expect(page.locator("[data-equalizer='bars']")).toBeVisible();
  await untilHeard(page);
  await page.getByRole("button", { name: "Готово" }).click();

  await expect(field(page)).toHaveValue(PHRASE);
  await expect(field(page)).toBeFocused();
  // Ни вложения, ни сообщения в ленте: звук ушёл только на распознавание.
  await expect(page.getByRole("list", { name: "Вложения" })).toHaveCount(0);
  await expect(page.getByRole("list", { name: "Сообщения" })).toHaveCount(0);

  // Человек правит и отправляет сам.
  await field(page).pressSequentially(" и послезавтра");
  await page.getByRole("button", { name: "Отправить" }).click();
  await expect(page.getByRole("list", { name: "Сообщения" })).toContainText(`${PHRASE} и послезавтра`);
});

test("если в поле уже был текст, распознанное добавляется через пробел", async ({ page }) => {
  await open(page, "/chat?thread=new&pace=0", "connected");
  await field(page).fill("Привет,");
  await mic(page).click();
  await expect(page.locator("[data-equalizer='bars']")).toBeVisible();
  await untilHeard(page);
  await page.getByRole("button", { name: "Готово" }).click();
  await expect(field(page)).toHaveValue(`Привет, ${PHRASE}`);
});

test("отмена ничего не делает: запрос на распознавание не уходит, поле как было", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/speech/transcribe")) requests.push(request.url());
  });
  await open(page, "/chat?thread=new&pace=0", "connected");
  await field(page).fill("черновик");
  await mic(page).click();
  await expect(page.locator("[data-equalizer='bars']")).toBeVisible();
  await page.getByRole("button", { name: "Отменить запись" }).click();
  await expect(page.locator("[data-recording]")).toHaveCount(0);
  await expect(field(page)).toHaveValue("черновик");
  await expect(page.getByRole("list", { name: "Вложения" })).toHaveCount(0);
  expect(requests).toEqual([]);
});

test("сокращённое движение: вместо столбиков — статичная полоса уровня", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await open(page, "/chat?thread=new&pace=0", "connected");
  await mic(page).click();
  await expect(page.locator("[data-equalizer='calm']")).toBeVisible();
  await expect(page.locator("[data-bar]")).toHaveCount(0);
  await page.getByRole("button", { name: "Отменить запись" }).click();
});

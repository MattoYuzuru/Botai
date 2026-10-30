import { expect, test } from "@playwright/test";
import { open } from "./support";

/** Настоящая запись звука в Chromium с поддельным микрофоном: `--use-fake-device-for-media-stream`. */
test.use({
  permissions: ["microphone"],
  launchOptions: { args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] },
});

test("настоящая запись MediaRecorder: индикатор, остановка, чип, отправка; агент честно говорит про расшифровку", async ({
  page,
}) => {
  await open(page, "/chat?thread=new&pace=0", "connected");
  await page.getByRole("button", { name: "Записать голосовое сообщение" }).click();
  await expect(page.getByText(/Идёт запись/)).toBeVisible();
  await page.waitForTimeout(700);
  await page.getByRole("button", { name: "Остановить и прикрепить" }).click();
  const chips = page.getByRole("list", { name: "Вложения" });
  await expect(chips.getByRole("button", { name: /^Открыть Голосовое сообщение/ })).toBeEnabled();
  await page.getByRole("button", { name: "Отправить" }).click();
  await expect(page.getByText(/расшифровки пока нет/)).toBeVisible();
  await page
    .getByRole("list", { name: "Сообщения" })
    .getByRole("button", { name: /^Открыть Голосовое сообщение/ })
    .click();
  await expect(page.getByRole("dialog").locator("audio")).toBeVisible();
});

test("отмена записи ничего не прикрепляет", async ({ page }) => {
  await open(page, "/chat?thread=new&pace=0", "connected");
  await page.getByRole("button", { name: "Записать голосовое сообщение" }).click();
  await expect(page.getByText(/Идёт запись/)).toBeVisible();
  await page.getByRole("button", { name: "Отменить запись" }).click();
  await expect(page.getByRole("list", { name: "Вложения" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Записать голосовое сообщение" })).toBeVisible();
});

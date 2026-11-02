import { expect, test } from "@playwright/test";
import { freezeClock, open } from "./data-support";

test("подключение нового вуза не теряет тап, когда загружаются подсказки", async ({ page }) => {
  await freezeClock(page);
  await open(page, "/chat?thread=new&pace=0", "connected");
  await page.getByRole("link", { name: "Подключить новый вуз", exact: true }).click();
  const composer = page.getByRole("textbox", { name: "Сообщение агенту" });
  await expect(composer).toHaveValue("Хочу подключить вуз. Название или ссылка на личный кабинет: ");
  await expect(composer).toBeFocused();
  await expect(page).toHaveURL(/thread=new/);
  await expect(page.locator("[data-role=user]")).toHaveCount(0);
  await composer.fill("Хочу подключить ЦУ");
  await expect(composer).toHaveValue("Хочу подключить ЦУ");
});

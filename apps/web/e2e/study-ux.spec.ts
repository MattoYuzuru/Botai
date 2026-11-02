import { expect, test } from "@playwright/test";
import { freezeClock, open } from "./data-support";

test.beforeEach(async ({ page }) => freezeClock(page));

test("подключённый аккаунт не получает приглашение подключить первый вуз", async ({ page }) => {
  for (const path of ["/", "/schedule", "/tasks"]) {
    await open(page, path, "connected");
    await expect(page.locator("[data-screen]").last()).toBeVisible();
    await expect(page.getByText("Подключим — и всё будет в одном месте")).toHaveCount(0);
    await expect(page.locator("[data-mascot-stage]")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Подключить вуз", exact: true })).toHaveCount(0);
  }
});

test("подсказка продолжения вставляется стрелкой или кнопкой и не отправляется сама", async ({
  page,
}, info) => {
  await open(page, "/chat?thread=new&pace=0", "connected");
  const input = page.getByRole("textbox", { name: "Сообщение агенту" });
  await input.fill("Какая следующая пара?");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  const ghost = page.locator("[data-composer-suggestion]");
  await expect(ghost).toBeVisible();
  const text = await ghost.innerText();
  await expect(input).toHaveValue("");
  const messages = page.getByRole("list", { name: "Сообщения" });
  const count = await messages.getByRole("listitem").count();
  if (info.project.name === "phone") {
    await page.getByRole("button", { name: "Вставить подсказку", exact: true }).click();
  } else {
    await input.press("ArrowRight");
  }
  await expect(input).toHaveValue(text);
  await expect(ghost).toHaveCount(0);
  await expect(messages.getByRole("listitem")).toHaveCount(count);
  await input.fill("Мой вопрос");
  await input.press("ArrowRight");
  await expect(input).toHaveValue("Мой вопрос");
});

test("пустой аккаунт сохраняет приглашение подключить вуз", async ({ page }) => {
  await open(page, "/tasks", "empty");
  await expect(page.getByRole("link", { name: "Подключить вуз", exact: true })).toBeVisible();
});

test("скрытие предупреждения сохраняется после перезагрузки, метки пар остаются", async ({ page }) => {
  await open(page, "/schedule", "connected");
  const warning = page.getByText("4 пары пересекаются по времени с парами другого вуза");
  await expect(warning).toBeVisible();
  await page.getByRole("button", { name: "Скрыть предупреждение о пересечениях" }).click();
  await expect(warning).toHaveCount(0);
  await page.reload();
  await expect(page.locator("[data-screen=schedule]")).toHaveAttribute("data-state", "ok");
  await expect(warning).toHaveCount(0);
  await expect(page.getByText(/^(Пересечение|Пересекается по времени)$/).first()).toBeVisible();
});

test("подключение нового вуза в пустом чате заполняет черновик без отправки", async ({ page }) => {
  await open(page, "/chat?thread=new&pace=0", "connected");
  await page.getByRole("link", { name: "Подключить новый вуз", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Сообщение агенту" })).toHaveValue(
    "Хочу подключить вуз. Название или ссылка на личный кабинет: ",
  );
  await expect(page.getByRole("list", { name: "Сообщения" }).getByRole("listitem")).toHaveCount(0);
});

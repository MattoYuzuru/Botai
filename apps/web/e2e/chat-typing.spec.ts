import { expect, test } from "@playwright/test";
import { open } from "./support";

/**
 * Печать живого ответа агента в настоящем браузере: без `?pace=0` мок-задание идёт «как для человека», поэтому
 * блок текста успевает прийти из потока задания и печатается. Проверяем суть эффекта: текст растёт ступенчато,
 * пока идёт печать — `streaming` и курсор; в конце — полный ответ, `sent` без курсора.
 */
test.describe("печать ответа агента", () => {
  test("ноутбук: текст растёт ступенчато, в конце курсор исчезает", async ({ page }, info) => {
    test.skip(info.project.name !== "laptop", "печать проверяется на большом экране");
    await open(page, "/chat?thread=new", "connected");
    const composer = page.getByRole("textbox", { name: "Сообщение агенту" });
    await expect(composer).toBeEnabled();
    await composer.fill("Какая следующая пара?");
    await page.getByRole("button", { name: "Отправить" }).click();

    const timeline = page.getByRole("list", { name: "Сообщения" });
    const answer = timeline.locator("[data-role='agent']").last();
    const markdown = answer.locator("[data-safe-markdown]");

    // Блок пришёл живым: сообщение сразу помечено streaming, в тексте стоит курсор.
    await expect(answer).toHaveAttribute("data-status", "streaming", { timeout: 20_000 });
    await expect(answer.locator("[data-caret]")).toHaveCount(1, { timeout: 5_000 });

    // Текст именно растёт, а не появился целиком: хотя бы две разные длины по ходу печати.
    const lengths = new Set<number>();
    await expect
      .poll(
        async () => {
          lengths.add((await markdown.textContent())?.length ?? 0);
          return lengths.size;
        },
        { timeout: 10_000 },
      )
      .toBeGreaterThan(1);

    // Печать закончилась: курсор ушёл, сообщение стало sent, ответ дорисован до таблицы.
    await expect(answer).toHaveAttribute("data-status", "sent", { timeout: 15_000 });
    await expect(answer.locator("[data-caret]")).toHaveCount(0);
    await expect(answer.getByRole("table")).toBeVisible();

    // Печать меняет высоту ленты, а человек её не прокручивал: страница осталась у низа.
    await expect
      .poll(() =>
        page.evaluate(() => {
          const root = document.scrollingElement as HTMLElement;
          return root.scrollHeight - root.scrollTop - root.clientHeight;
        }),
      )
      .toBeLessThanOrEqual(1);
  });
});

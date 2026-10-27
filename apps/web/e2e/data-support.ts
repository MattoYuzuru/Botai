import { expect, type Page } from "@playwright/test";

export { findSmallTapTargets, findTinyText, open } from "./support";

/**
 * Документ не шире заданной ширины. В мобильной эмуляции (`isMobile`) браузер при переполнении сам
 * расширяет окно просмотра, и `innerWidth` перестаёт быть мерой, поэтому сравниваем с заказанной шириной.
 */
export async function expectFitsWidth(page: Page, width: number, label = "") {
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(scrollWidth, `горизонтальная прокрутка ${label}`).toBeLessThanOrEqual(width);
}

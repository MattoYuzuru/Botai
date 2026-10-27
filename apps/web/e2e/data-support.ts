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

/** «Сейчас» e2e экранов данных: вторник, 27 октября 2026, 17:40 по Москве — данные мока строятся от него. */
export const FIXED_NOW = new Date("2026-10-27T14:40:00Z");

/** Часы страницы стоят на `FIXED_NOW`, таймеры идут как обычно: группы, срочность и «Идёт сейчас» не зависят от времени суток. */
export async function freezeClock(page: Page) {
  await page.clock.setFixedTime(FIXED_NOW);
}

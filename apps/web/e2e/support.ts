import { expect, type Page } from "@playwright/test";

export const DEMO = {
  email: "student@example.test",
  password: "correct-horse-battery",
  invite: "BOTAI-DEMO",
} as const;

export type Scenario = "fresh" | "empty" | "connected" | "reauth" | "stale";

/** Открывает страницу мок-приложения в нужном сценарии (задержка 0 — тесты быстрые). */
export async function open(page: Page, path: string, scenario: Scenario = "fresh") {
  const separator = path.includes("?") ? "&" : "?";
  await page.goto(`${path}${separator}scenario=${scenario}&latency=0`);
}

export async function signIn(page: Page) {
  await page.getByLabel("Почта", { exact: true }).fill(DEMO.email);
  await page.getByLabel("Пароль", { exact: true }).fill(DEMO.password);
  await page.getByRole("button", { name: "Войти" }).click();
}

/** Документ не прокручивается по горизонтали. */
export async function expectNoHorizontalOverflow(page: Page, label = "") {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, `горизонтальная прокрутка ${label}`).toBeLessThanOrEqual(0);
}

/** Снимок панели — после её штатного въезда, когда она целиком находится во viewport. */
export async function expectSheetReady(page: Page, title: string) {
  const sheet = page.getByRole("dialog", { name: title });
  const viewport = page.viewportSize();
  if (!viewport) throw new Error("Нет размера viewport");
  await expect(sheet).toBeVisible();
  await expect
    .poll(async () => {
      const box = await sheet.boundingBox();
      return box ? box.x + box.width : Number.POSITIVE_INFINITY;
    })
    .toBeLessThanOrEqual(viewport.width);
  await expect
    .poll(async () => {
      const box = await sheet.boundingBox();
      return box ? box.y + box.height : Number.POSITIVE_INFINITY;
    })
    .toBeLessThanOrEqual(viewport.height);
}

/**
 * Интерактивные элементы с зоной нажатия меньше 44×44 px. Расширение через `::before` учитывается
 * попаданием `elementFromPoint`; ссылка внутри предложения — часть текста (WCAG 2.5.8) и пропускается.
 */
export async function findSmallTapTargets(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const found: string[] = [];
    const selector = "button, a[href], [role=checkbox], [role=radio], [role=switch], [role=combobox]";
    const reaches = (element: Element, x: number, y: number) => {
      const hit = document.elementFromPoint(x, y);
      return Boolean(hit && (hit === element || element.contains(hit)));
    };
    for (const element of document.querySelectorAll<HTMLElement>(selector)) {
      if (getComputedStyle(element).display === "inline") continue;
      const target = element.closest("label") ?? element;
      target.scrollIntoView({ block: "center", inline: "center" });
      const box = target.getBoundingClientRect();
      if (box.width === 0 || box.height === 0) continue;
      const centerX = box.left + box.width / 2;
      const centerY = box.top + box.height / 2;
      const growY = Math.max(0, (44 - box.height) / 2);
      const growX = Math.max(0, (44 - box.width) / 2);
      const okY =
        growY === 0 ||
        (reaches(target, centerX, box.top - growY + 1) && reaches(target, centerX, box.bottom + growY - 1));
      const okX =
        growX === 0 ||
        (reaches(target, box.left - growX + 1, centerY) && reaches(target, box.right + growX - 1, centerY));
      if (!okX || !okY) {
        const name = (element.textContent || element.getAttribute("aria-label") || "").trim().slice(0, 30);
        found.push(`${element.tagName} «${name}» ${Math.round(box.width)}×${Math.round(box.height)}`);
      }
    }
    return found;
  });
}

/** Любой текст на странице не меньше 13 px. */
export async function findTinyText(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const found: string[] = [];
    for (const element of document.querySelectorAll<HTMLElement>("body *")) {
      if (![...element.childNodes].some((node) => node.nodeType === 3 && node.textContent?.trim())) continue;
      if (element.closest(".sr-only")) continue;
      const size = Number.parseFloat(getComputedStyle(element).fontSize);
      if (size < 12.99) found.push(`${element.tagName}.${element.className}: ${size}`);
    }
    return found;
  });
}

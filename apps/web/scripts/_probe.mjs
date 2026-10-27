import { chromium } from "@playwright/test";

const [path = "/?scenario=connected&latency=0", width = "320", ready = "[data-screen=today][data-state=ok]"] =
  process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: Number(width), height: 800 } });
await page.goto(`http://127.0.0.1:5301${path}`);
await page.waitForSelector(ready);
const out = await page.evaluate(() => {
  const w = window.innerWidth;
  return [...document.querySelectorAll("body *")]
    .map((el) => ({ el, r: el.getBoundingClientRect() }))
    .filter(({ r }) => r.right > w + 0.5 && r.width > 0)
    .map(
      ({ el, r }) =>
        `${el.tagName}.${String(el.className).slice(0, 70)} right=${Math.round(r.right)} w=${Math.round(r.width)} :: ${(el.textContent || "").slice(0, 30)}`,
    )
    .slice(0, 15);
});
console.log(out.join("\n"));
console.log("scrollWidth", await page.evaluate(() => document.documentElement.scrollWidth));
await browser.close();

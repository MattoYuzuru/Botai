import { defineConfig, devices } from "@playwright/test";

/**
 * e2e идут против dev-сервера Vite на моках (VITE_MOCKS=1): бэкенд не нужен. В CI тот же job `web`
 * должен поставить браузер (`playwright install --with-deps chromium`) — версия Playwright закреплена
 * в package.json (1.63.0), потому что другой Chromium в окружении разработки недоступен.
 */
const PORT = Number(process.env.E2E_PORT ?? 5199);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? `http://127.0.0.1:${PORT}`,
    locale: "ru-RU",
    timezoneId: "Europe/Moscow",
    trace: "retain-on-failure",
  },
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `VITE_MOCKS=1 pnpm exec vite --host 127.0.0.1 --port ${PORT} --strictPort`,
        url: `http://127.0.0.1:${PORT}`,
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
  projects: [
    { name: "phone", use: { ...devices["Pixel 7"], viewport: { width: 390, height: 844 } } },
    { name: "laptop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
  ],
});

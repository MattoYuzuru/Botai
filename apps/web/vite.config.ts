import tailwindcss from "@tailwindcss/vite";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Целевые браузеры: iOS Safari 17.5+, Android Chrome 123+ и свежие настольные. На них нативно работает
 * light-dark() — на нём построены темы токенов. Для более старых (Safari 16.4–17.4) в tokens.css
 * есть запасной блок: светлая тема. Если понизить цель, Lightning CSS заменит light-dark() на
 * переменные-переключатели, и вложенные темы (витрина /styleguide) перестанут работать.
 */
const TARGETS = ["es2023", "chrome123", "safari17.5", "firefox120"];

export default defineConfig({
  plugins: [TanStackRouterVite({ target: "react", autoCodeSplitting: true }), react(), tailwindcss()],
  resolve: {
    alias: { "@": new URL("./src", import.meta.url).pathname },
    // Одна копия React на приложение и дизайн-систему: две копии дают «Invalid hook call».
    dedupe: ["react", "react-dom"],
  },
  // Зависимости собираем заранее: иначе Vite находит их во время работы, пересобирает и перезагружает
  // страницу, и в браузер попадает смесь старых и новых чанков (две копии React в первые секунды).
  optimizeDeps: {
    include: [
      "react",
      "react-dom",
      "react-dom/client",
      "react/jsx-runtime",
      "react/jsx-dev-runtime",
      "zustand",
      "zustand/middleware",
      "openapi-fetch",
      "@tanstack/react-query",
      "@tanstack/react-router",
      "@botai/design > radix-ui",
      "@botai/design > sonner",
      "@botai/design > clsx",
      "@botai/design > class-variance-authority",
      "@botai/design > tailwind-merge",
      "@botai/design > @tabler/icons-react",
    ],
  },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true, ws: true } },
  },
  build: { target: TARGETS, cssTarget: TARGETS.slice(1) },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // Витрина /styleguide рендерит весь каталог: в CI (медленный раннер) 5 с по умолчанию не хватает.
    testTimeout: 30_000,
  },
});

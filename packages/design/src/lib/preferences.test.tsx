import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AccentScope } from "../ui/accent-scope/accent-scope";
import { useCalmMotion, useNow } from "./hooks";
import {
  applyMotion,
  applyTheme,
  effectiveTheme,
  initPreferences,
  motionPreference,
  STORAGE_KEYS,
  THEME_COLORS,
  themePreference,
} from "./preferences";

const root = () => document.documentElement;

beforeEach(() => {
  window.localStorage.clear();
  root().removeAttribute("data-theme");
  root().removeAttribute("data-motion");
  for (const meta of document.querySelectorAll('meta[name="theme-color"]')) meta.remove();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("тема", () => {
  it("системная тема — без атрибута; явный выбор ставит data-theme и цвет строки состояния", () => {
    applyTheme("dark");
    expect(root()).toHaveAttribute("data-theme", "dark");
    expect(document.querySelector('meta[name="theme-color"]')).toHaveAttribute("content", THEME_COLORS.dark);
    applyTheme("light");
    expect(root()).toHaveAttribute("data-theme", "light");
    expect(document.querySelector('meta[name="theme-color"]')).toHaveAttribute("content", THEME_COLORS.light);
    applyTheme("system");
    expect(root()).not.toHaveAttribute("data-theme");
  });

  it("системная тема берётся из prefers-color-scheme", () => {
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query) =>
        ({
          matches: query.includes("dark"),
          media: query,
          addEventListener() {},
          removeEventListener() {},
        }) as unknown as MediaQueryList,
    );
    expect(effectiveTheme("system")).toBe("dark");
    expect(effectiveTheme("light")).toBe("light");
  });

  it("выбор хранится локально; значение по умолчанию ничего не пишет", () => {
    themePreference.write("dark");
    expect(window.localStorage.getItem(STORAGE_KEYS.theme)).toBe("dark");
    expect(themePreference.read()).toBe("dark");
    themePreference.write("system");
    expect(window.localStorage.getItem(STORAGE_KEYS.theme)).toBeNull();
  });

  it("мусор в хранилище не ломает чтение", () => {
    window.localStorage.setItem(STORAGE_KEYS.theme, "neon");
    expect(themePreference.read()).toBe("system");
  });

  it("недоступное хранилище не роняет приложение", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => themePreference.write("dark")).not.toThrow();
    // Настройка живёт в памяти до перезагрузки.
    expect(themePreference.read()).toBe("dark");
    expect(() => themePreference.write("system")).not.toThrow();
    expect(themePreference.read()).toBe("system");
  });

  it("initPreferences применяет сохранённое и реагирует на смену настройки", () => {
    window.localStorage.setItem(STORAGE_KEYS.theme, "dark");
    const off = initPreferences();
    expect(root()).toHaveAttribute("data-theme", "dark");
    act(() => themePreference.write("light"));
    expect(root()).toHaveAttribute("data-theme", "light");
    off();
    act(() => themePreference.write("dark"));
    expect(root()).toHaveAttribute("data-theme", "light");
  });

  it("скрытая вкладка помечает корень для остановки декоративных циклов", () => {
    const off = initPreferences();
    expect(root()).toHaveAttribute("data-hidden", "false");
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(root()).toHaveAttribute("data-hidden", "true");
    off();
  });
});

describe("спокойное движение", () => {
  it("data-motion=calm ставится и снимается", () => {
    applyMotion("calm");
    expect(root()).toHaveAttribute("data-motion", "calm");
    applyMotion("system");
    expect(root()).not.toHaveAttribute("data-motion");
  });

  it("useCalmMotion: настройка приложения или системный reduced-motion", () => {
    const { result } = renderHook(() => useCalmMotion());
    expect(result.current).toBe(false);
    act(() => motionPreference.write("calm"));
    expect(result.current).toBe(true);
    act(() => motionPreference.write("system"));
    expect(result.current).toBe(false);

    vi.spyOn(window, "matchMedia").mockImplementation(
      (query) =>
        ({
          matches: query.includes("reduce"),
          media: query,
          addEventListener() {},
          removeEventListener() {},
        }) as unknown as MediaQueryList,
    );
    const system = renderHook(() => useCalmMotion());
    expect(system.result.current).toBe(true);
  });
});

describe("useNow", () => {
  it("override даёт одинаковый экран при одинаковом времени", () => {
    const { result } = renderHook(() => useNow(1000, 42));
    expect(result.current).toBe(42);
  });

  it("тикает раз в интервал, пока вкладка видна", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-27T11:00:00Z"));
    const { result } = renderHook(() => useNow(1000));
    const first = result.current;
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(result.current - first).toBe(3000);
  });
});

describe("AccentScope: акцент пространства", () => {
  it("тон из реестра ставится атрибутом data-space", () => {
    render(
      <AccentScope hue={292}>
        <span>ЦУ</span>
      </AccentScope>,
    );
    expect(screen.getByText("ЦУ").parentElement).toHaveAttribute("data-space", "cu");
  });

  it("неизвестный допустимый тон — вычисленные переменные акцента, только --color-accent*", () => {
    render(
      <AccentScope hue={300}>
        <span>вуз</span>
      </AccentScope>,
    );
    const scope = screen.getByText("вуз").parentElement as HTMLElement;
    expect(scope).toHaveAttribute("data-space", "custom");
    const names = Array.from({ length: scope.style.length }, (_, index) => scope.style.item(index));
    expect(names.sort()).toEqual([
      "--color-accent",
      "--color-accent-fg",
      "--color-accent-hover",
      "--color-accent-subtle",
    ]);
    expect(scope.style.getPropertyValue("--color-accent")).toMatch(/^oklch\(0\.52 /);
  });

  it("тон вне окна и отсутствующий тон — акцент ВШЭ", () => {
    const { rerender } = render(
      <AccentScope hue={80}>
        <span>вуз</span>
      </AccentScope>,
    );
    expect(screen.getByText("вуз").parentElement).toHaveAttribute("data-space", "hse");
    rerender(
      <AccentScope hue={null}>
        <span>вуз</span>
      </AccentScope>,
    );
    expect(screen.getByText("вуз").parentElement).toHaveAttribute("data-space", "hse");
    expect((screen.getByText("вуз").parentElement as HTMLElement).style.length).toBe(0);
  });
});

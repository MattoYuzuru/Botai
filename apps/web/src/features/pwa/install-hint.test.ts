import { describe, expect, it } from "vitest";
import { isIos, markInstallHintShown, shouldShowInstallHint, wasInstallHintShown } from "./install-hint";

const IPHONE =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";
const ANDROID =
  "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 Chrome/123.0 Mobile Safari/537.36";
const MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15";

const env = (patch: Partial<Parameters<typeof shouldShowInstallHint>[0]>) => ({
  userAgent: IPHONE,
  platform: "iPhone",
  maxTouchPoints: 5,
  standalone: false,
  ...patch,
});

describe("подсказка «Добавить на экран „Домой“»", () => {
  it("iPhone и iPad — iOS; iPadOS под видом Mac — тоже, обычный Mac и Android — нет", () => {
    expect(isIos(env({}))).toBe(true);
    expect(isIos(env({ userAgent: MAC, platform: "MacIntel", maxTouchPoints: 5 }))).toBe(true);
    expect(isIos(env({ userAgent: MAC, platform: "MacIntel", maxTouchPoints: 0 }))).toBe(false);
    expect(isIos(env({ userAgent: ANDROID, platform: "Linux armv8l" }))).toBe(false);
  });

  it("показывается на iOS во вкладке браузера, если ещё не показывалась", () => {
    expect(shouldShowInstallHint(env({}), false)).toBe(true);
  });

  it("не показывается, если приложение уже установлено, показывалось или это не iOS (там есть системный диалог)", () => {
    expect(shouldShowInstallHint(env({ standalone: true }), false)).toBe(false);
    expect(shouldShowInstallHint(env({}), true)).toBe(false);
    expect(shouldShowInstallHint(env({ userAgent: ANDROID, platform: "Linux armv8l" }), false)).toBe(false);
  });

  it("«показано» запоминается один раз; недоступное хранилище — подсказка не показывается, а не спамит", () => {
    const rows = new Map<string, string>();
    const storage = {
      getItem: (key: string) => rows.get(key) ?? null,
      setItem: (key: string, value: string) => void rows.set(key, value),
    };
    expect(wasInstallHintShown(storage)).toBe(false);
    markInstallHintShown(storage);
    expect(wasInstallHintShown(storage)).toBe(true);
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(wasInstallHintShown(broken)).toBe(true);
    expect(() => markInstallHintShown(broken)).not.toThrow();
  });
});

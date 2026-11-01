import { describe, expect, it } from "vitest";
import { TYPEWRITER_CPS, TYPEWRITER_MAX_MS, TYPEWRITER_MIN_MS, typingDurationMs } from "./typing";

/**
 * Скорость печати — часть UX-контракта (styleguide: «чат», «Печать ответа»): короткая реплика должна
 * читаться как печать, а длинный ответ не должен задерживать человека дольше четырёх секунд.
 */
describe("длительность печати ответа", () => {
  it("пустой текст печатать нечего", () => {
    expect(typingDurationMs(0)).toBe(0);
  });

  it("короткая реплика держится минимум, обычная — по скорости, длинная упирается в потолок", () => {
    expect(TYPEWRITER_CPS).toBe(320);
    expect(typingDurationMs(1)).toBe(TYPEWRITER_MIN_MS);
    // 320 code points — ровно секунда при базовой скорости.
    expect(typingDurationMs(TYPEWRITER_CPS)).toBe(1000);
    expect(typingDurationMs(TYPEWRITER_CPS * 100)).toBe(TYPEWRITER_MAX_MS);
    expect(TYPEWRITER_MIN_MS).toBeLessThan(1000);
    expect(TYPEWRITER_MAX_MS).toBe(4000);
  });

  it("растёт монотонно: длиннее текст — не короче печать", () => {
    let previous = typingDurationMs(0);
    for (const chars of [1, 10, 32, 64, 100, 160, 320, 640, 1280, 2560, 5120, 20000]) {
      const current = typingDurationMs(chars);
      expect(current).toBeGreaterThanOrEqual(previous);
      expect(current).toBeLessThanOrEqual(TYPEWRITER_MAX_MS);
      previous = current;
    }
  });
});

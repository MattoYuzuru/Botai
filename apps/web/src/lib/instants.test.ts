import { describe, expect, it } from "vitest";
import { compareInstants } from "./instants";

describe("точность времени сообщений", () => {
  it("различает сообщения внутри одной миллисекунды", () => {
    expect(compareInstants("2026-11-01T10:00:00.344611Z", "2026-11-01T10:00:00.344609Z")).toBeGreaterThan(0);
    expect(compareInstants("2026-11-01T10:00:00.344609Z", "2026-11-01T10:00:00.344611Z")).toBeLessThan(0);
  });
  it("учитывает часовой пояс и разное число десятичных знаков", () => {
    expect(compareInstants("2026-11-01T13:00:00.344610+03:00", "2026-11-01T10:00:00.34461Z")).toBe(0);
    expect(compareInstants("2026-11-01T10:00:00.344Z", "2026-11-01T10:00:00.344001Z")).toBeLessThan(0);
    expect(compareInstants("2026-11-01T10:00:01Z", "2026-11-01T10:00:00.999999Z")).toBeGreaterThan(0);
  });
});

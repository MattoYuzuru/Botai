import { describe, expect, it } from "vitest";
import { safeAppHref } from "./safe-url";

describe("safeAppHref", () => {
  it("путь приложения, якорь и http(s) проходят", () => {
    expect(safeAppHref("/chat?thread=T1")).toBe("/chat?thread=T1");
    expect(safeAppHref("#plan")).toBe("#plan");
    expect(safeAppHref("https://lk.fakeuni.test/x")).toBe("https://lk.fakeuni.test/x");
  });

  it("javascript:, data:, mailto:, //host и обратная косая черта — нет", () => {
    for (const href of [
      "javascript:alert(1)",
      "data:text/html,x",
      "mailto:a@b.test",
      "//evil.test",
      "/\\evil.test",
      "",
      "botai://threads/1",
    ])
      expect(safeAppHref(href), href).toBeNull();
  });
});

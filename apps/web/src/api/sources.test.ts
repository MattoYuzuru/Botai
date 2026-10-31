import { describe, expect, it } from "vitest";
import { ApiError, NetworkError } from "./errors";
import { isSourcesApiUnavailable, sourceErrorOf } from "./sources";

describe("SourceOut: ошибка источника", () => {
  it("берёт код и человеческое описание, не отдавая секретов", () => {
    expect(sourceErrorOf({ lastError: { code: "source_auth_required", detail: "  Нужен вход. " } })).toEqual({
      code: "source_auth_required",
      detail: "Нужен вход.",
    });
  });

  it("пустая ошибка не превращается в текст; мусорные поля не ломают экран", () => {
    expect(sourceErrorOf({ lastError: null })).toEqual({ code: null, detail: null });
    expect(sourceErrorOf({ lastError: {} })).toEqual({ code: null, detail: null });
    expect(sourceErrorOf({ lastError: { detail: 42 } })).toEqual({ code: null, detail: null });
  });
});

describe("отсутствие v4-API", () => {
  it("404 и 501 — выключенный API, а не сбой; сеть и 5xx — настоящая ошибка", () => {
    expect(isSourcesApiUnavailable(new ApiError(404, "not_found", ""))).toBe(true);
    expect(isSourcesApiUnavailable(new ApiError(501, "not_implemented", ""))).toBe(true);
    expect(isSourcesApiUnavailable(new ApiError(500, "internal_error", ""))).toBe(false);
    expect(isSourcesApiUnavailable(new NetworkError())).toBe(false);
    expect(isSourcesApiUnavailable(new Error("boom"))).toBe(false);
  });
});

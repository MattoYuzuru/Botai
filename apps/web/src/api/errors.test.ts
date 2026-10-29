import { describe, expect, it } from "vitest";
import { ApiError, humanMessage, isClientError, isUnauthorized, NetworkError, parseProblem } from "./errors";

describe("problem+json", () => {
  it("разбирает код и текст для человека", () => {
    const error = parseProblem(401, {
      type: "https://botai.mnema.app/errors/unauthorized",
      title: "unauthorized",
      status: 401,
      code: "unauthorized",
      detail: "Неверная почта или пароль",
      traceId: "abc",
    });
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("unauthorized");
    expect(error.detail).toBe("Неверная почта или пароль");
    expect(isUnauthorized(error)).toBe(true);
  });

  it("сохраняет дополнительные поля (например, host у egress_blocked)", () => {
    const error = parseProblem(403, { code: "egress_blocked", detail: "Хост вне списка", host: "evil.test" });
    expect(error.extra).toEqual({ host: "evil.test" });
  });

  it("неизвестное тело и список ошибок валидации не превращаются в технический текст", () => {
    expect(parseProblem(502, "<html>").detail).toBe("");
    const validation = parseProblem(422, {
      detail: [{ loc: ["body", "email"], msg: "value is not a valid email" }],
    });
    expect(validation.detail).toBe("");
    expect(humanMessage(validation)).toBe("Не получилось. Попробуйте ещё раз позже.");
  });
});

describe("человеческие тексты", () => {
  it("сеть, квота и сбои источника — без технических слов", () => {
    expect(humanMessage(new NetworkError())).toMatch(/Нет связи/);
    expect(humanMessage(new ApiError(429, "quota_exceeded", ""))).toMatch(/лимит/);
    expect(humanMessage(new ApiError(502, "source_unavailable", ""))).toMatch(/Сайт вуза/);
    expect(humanMessage(new ApiError(500, "boom", ""))).toMatch(/неполадка/);
    for (const code of [
      "quota_exceeded",
      "source_unavailable",
      "source_rate_limited",
      "source_auth_required",
      "validation",
    ]) {
      expect(humanMessage(new ApiError(400, code, ""))).not.toMatch(/токен|сессия|API|парсер|http/i);
    }
  });

  it("текст сервера важнее заготовки", () => {
    expect(humanMessage(new ApiError(409, "conflict", "Эта почта уже зарегистрирована"))).toBe(
      "Эта почта уже зарегистрирована",
    );
  });

  it.each([
    ["source_auth_required", "Нужно снова войти в личный кабинет вуза."],
    [
      "recipe_rejected",
      "Описание подключения не прошло проверку или не было подтверждено. Подключение не завершено.",
    ],
    [
      "recipe_drift",
      "Данные сайта больше не соответствуют описанию подключения. Нужно заново проверить источник.",
    ],
    ["egress_blocked", "Переход на другой адрес сайта не разрешён для этого подключения."],
    ["source_bot_protection", "Сайт вуза требует проверку человеком. Пройдите её на экране входа."],
    ["llm_unavailable", "Модель сейчас недоступна. Не удалось продолжить работу с подключением."],
    ["login_failed", "Вход не завершён. Можно войти заново."],
    [
      "injection_blocked",
      "Инструкция со страницы отклонена. Содержимое сайта используется только как данные.",
    ],
  ])("%s объясняет причину подключения, даже если история использует статус 500", (code, message) => {
    expect(humanMessage(new ApiError(500, code, ""))).toBe(message);
  });
});

describe("повторы в фоне", () => {
  it("ответ 4xx не повторяем, сбой связи и 5xx — повторяем", () => {
    expect(isClientError(new ApiError(404, "not_found", ""))).toBe(true);
    expect(isClientError(new ApiError(401, "unauthorized", ""))).toBe(true);
    expect(isClientError(new ApiError(429, "quota_exceeded", ""))).toBe(false);
    expect(isClientError(new ApiError(503, "x", ""))).toBe(false);
    expect(isClientError(new NetworkError())).toBe(false);
  });
});

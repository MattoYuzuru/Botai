/**
 * Ошибки API. Тело ошибки — `application/problem+json` с машинным `code` (contracts §7);
 * `detail` написан для человека, технических слов в нём нет.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly extra: Record<string, unknown>;

  constructor(status: number, code: string, detail: string, extra: Record<string, unknown> = {}) {
    super(`${code}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.extra = extra;
  }
}

/** Запрос не дошёл до сервера: нет сети, обрыв, таймаут. Повторы идут в фоне — кнопки «Повторить» нет. */
export class NetworkError extends Error {
  constructor(cause?: unknown) {
    super("network");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

export const isApiError = (error: unknown): error is ApiError => error instanceof ApiError;
export const isNetworkError = (error: unknown): error is NetworkError => error instanceof NetworkError;
export const isUnauthorized = (error: unknown): boolean => isApiError(error) && error.status === 401;

/** Ошибки, которые повторять бессмысленно: ответ сервера, а не сбой связи. */
export const isClientError = (error: unknown): boolean =>
  isApiError(error) && error.status >= 400 && error.status < 500 && error.status !== 429;

/** Разбор тела problem+json. Неизвестное тело превращается в нейтральный текст без технических деталей. */
export function parseProblem(status: number, body: unknown): ApiError {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const code = typeof record.code === "string" ? record.code : "unknown";
    let detail = typeof record.detail === "string" ? record.detail : "";
    // Ответ валидации FastAPI по умолчанию: detail — список ошибок полей; человеку он не показывается.
    if (Array.isArray(record.detail)) detail = "";
    const {
      type: _type,
      title: _title,
      status: _status,
      code: _code,
      detail: _detail,
      traceId: _trace,
      ...extra
    } = record;
    return new ApiError(status, code, detail, extra);
  }
  return new ApiError(status, "unknown", "");
}

/** Что показать человеку по коду ошибки (contracts §7 → styleguide §9). Технических кодов в тексте нет. */
export function humanMessage(error: unknown): string {
  if (isNetworkError(error)) return "Нет связи с BotAI. Данные обновятся, когда сеть вернётся.";
  if (!isApiError(error)) return "Что-то пошло не так. Попробуйте позже.";
  if (error.detail) return error.detail;
  switch (error.code) {
    case "unauthorized":
      return "Нужно войти в BotAI.";
    case "forbidden":
      return "Это действие вам недоступно.";
    case "not_found":
      return "Не нашли то, что вы ищете.";
    case "quota_exceeded":
      return "На сегодня лимит ответов исчерпан. Он обновится завтра.";
    case "validation":
      return "Проверьте заполненные поля.";
    case "source_unavailable":
      return "Сайт вуза сейчас не отвечает. Данные обновятся, когда он вернётся.";
    case "source_rate_limited":
      return "Вуз просит подождать. Данные обновятся позже.";
    case "source_auth_required":
      return "Нужно снова войти в личный кабинет вуза.";
    case "source_bot_protection":
      return "Сайт вуза требует проверку человеком. Пройдите её на экране входа.";
    case "login_failed":
      return "Вход не завершён. Можно войти заново.";
    case "recipe_rejected":
      return "Описание подключения не прошло проверку или не было подтверждено. Подключение не завершено.";
    case "recipe_drift":
      return "Данные сайта больше не соответствуют описанию подключения. Нужно заново проверить источник.";
    case "egress_blocked":
      return "Переход на другой адрес сайта не разрешён для этого подключения.";
    case "injection_blocked":
      return "Инструкция со страницы отклонена. Содержимое сайта используется только как данные.";
    case "llm_unavailable":
      return "Модель сейчас недоступна. Не удалось продолжить работу с подключением.";
    default:
      return error.status >= 500
        ? "У BotAI сейчас неполадка. Мы уже разбираемся."
        : "Не получилось. Попробуйте ещё раз позже.";
  }
}

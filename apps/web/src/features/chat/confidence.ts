/**
 * Честная пометка «Не уверен». В `TextBlock` нет поля уверенности (контракт `agent-ui.schema.json`), поэтому клиент
 * узнаёт её по тому, что агент сам обязан сказать (`prompts/system.md`, раздел «Честность»): «Не знаю: …» — нет
 * данных, источника или основания; «Ответ может быть неполным» — сервер остановил агента по лимиту шагов.
 * Живой ответ дополнительно проверяется по итогу задания (`result.stopped`). Ничего сверх этого не угадывается:
 * уверенный по виду ответ не помечается.
 */
const UNSURE_START = /^\s*Не знаю\s*[:,.—-]/i;
const INCOMPLETE = /Ответ может быть неполным/;

export type Confidence = "ok" | "unsure";

export function textConfidence(markdown: string): Confidence {
  return UNSURE_START.test(markdown) || INCOMPLETE.test(markdown) ? "unsure" : "ok";
}

/** Итог задания: агент остановился не ответом (лимит шагов, квота, ошибка) — ответ неполный. */
export function resultConfidence(result: Record<string, unknown> | null | undefined): Confidence {
  const stopped = result?.stopped;
  return typeof stopped === "string" && stopped !== "answer" ? "unsure" : "ok";
}

import type { Choice, Confirm, Input } from "@/api/agent-ui.gen";
import { isAuthInput } from "@/features/jobs/job-state";

/**
 * Что в ленте вместо ответа на вопрос задачи, пока сервер не прислал своё сообщение: то же, что запишет он.
 * Выбор и согласие — слова варианта, пароль, код и любое поле входа — только отметка, значения нет.
 */
export function answerMark(block: Input | Choice | Confirm, shown: string | null): string {
  if (block.type !== "input") return shown ?? "Ответ передан";
  if (block.inputKind === "password") return "Пароль передан";
  if (block.inputKind === "code") return "Код передан";
  if (block.secret || isAuthInput(block))
    return block.autocomplete === "username" || block.inputKind === "email"
      ? "Логин передан"
      : "Ответ передан";
  return shown ?? "Ответ передан";
}

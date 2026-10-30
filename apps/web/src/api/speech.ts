/**
 * Диктовка (`docs/development/speech-to-text.md`): запись голоса уходит на `POST /api/v1/speech/transcribe`, ответ —
 * текст для поля ввода. Звук нигде не сохраняется и во вложения не попадает.
 */
import type { Transcribe } from "@botai/design";
import { humanMessage } from "./errors";
import type { components } from "./schema.gen";
import { postForm } from "./v3";

export type TranscriptOut = components["schemas"]["TranscriptOut"];

/**
 * Распознаёт запись. Отказ — `Error` с текстом для человека (сообщение сервера или общая фраза по коду): диктовка в
 * поле ввода показывает его как есть. Отмена (`signal`) пробрасывается как `AbortError`.
 */
export const transcribeSpeech: Transcribe = async (audio, { signal, durationMs }) => {
  const form = new FormData();
  form.set("audio", audio, audio.name);
  form.set("durationMs", String(Math.max(0, Math.round(durationMs))));
  try {
    const { text } = await postForm<TranscriptOut>("/api/v1/speech/transcribe", form, { signal });
    return text;
  } catch (error) {
    if (signal.aborted) throw error;
    throw new Error(humanMessage(error));
  }
};

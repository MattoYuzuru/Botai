import { formatBytes, safeHttpUrl } from "@botai/design";
import { api, call } from "@/api/client";
import type { DocumentStarted } from "@/features/data/types";

/** Файл до 8 МБ: PDF, DOCX, XLSX, CSV, HTML и текст (так же, как проверяет сервер). */
export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;
export const UPLOAD_ACCEPT = ".pdf,.docx,.xlsx,.csv,.html,.htm,.txt,.md";
const UPLOAD_EXT = /\.(pdf|docx|xlsx|csv|html?|txt|md)$/i;

/** Ошибка поля «Ссылка» словами для человека; `null` — ссылка годится. */
export function validateUrl(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return "Вставьте ссылку на документ или страницу";
  if (!safeHttpUrl(trimmed)) return "Нужна ссылка вида https://…";
  return null;
}

/** Ошибка выбора файла до отправки: тип и размер. */
export function validateFile(file: File): string | null {
  if (!UPLOAD_EXT.test(file.name)) {
    return "Этот тип файла не поддерживается. Подойдут PDF, DOCX, XLSX, CSV, HTML и текст.";
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return `Файл ${formatBytes(file.size)} больше ${formatBytes(MAX_UPLOAD_BYTES)}. Уменьшите его или дайте ссылку.`;
  }
  if (file.size === 0) return "Файл пустой.";
  return null;
}

export function startUrlDocument(
  spaceId: string,
  input: { url: string; title?: string; note?: string },
): Promise<DocumentStarted> {
  return call(
    api.POST("/api/v1/spaces/{space_id}/documents/url", {
      params: { path: { space_id: spaceId } },
      body: {
        url: input.url.trim(),
        title: input.title?.trim() || null,
        note: input.note?.trim() || null,
      },
    }),
  );
}

export function startUploadDocument(spaceId: string, file: File, note?: string): Promise<DocumentStarted> {
  return call(
    api.POST("/api/v1/spaces/{space_id}/documents/upload", {
      params: { path: { space_id: spaceId } },
      // Тип тела в схеме — строка; на деле это multipart: файл и необязательная заметка.
      body: { file: "" },
      bodySerializer: () => {
        const form = new FormData();
        form.append("file", file);
        if (note?.trim()) form.append("note", note.trim());
        return form;
      },
    }),
  );
}

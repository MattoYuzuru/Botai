import { type ComposerAttachment, type FileSource, toast, type ViewerAttachment } from "@botai/design";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { humanMessage } from "@/api/errors";
import {
  type AttachmentKind,
  type AttachmentOut,
  attachmentContentUrl,
  MAX_ATTACHMENT_BYTES,
  MAX_MESSAGE_ATTACHMENTS,
  uploadAttachment,
  v3,
} from "@/api/v3";
import { clientId } from "@/lib/client-id";
import type { OutboxAttachment } from "./outbox";

const DOCUMENT_MIME = /^(application\/(pdf|msword|vnd\.|rtf)|text\/)/;

/** Вид файла по типу: чип показывает его сразу, до ответа сервера (сервер потом присылает свой `kind`). */
export function kindOfFile(file: Pick<File, "type" | "name">): AttachmentKind {
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("audio/")) return "audio";
  if (DOCUMENT_MIME.test(file.type) || /\.(pdf|docx?|xlsx?|csv|tsv|txt|md|html?)$/i.test(file.name))
    return "document";
  return "other";
}

interface Draft extends ComposerAttachment {
  file: File;
  out?: AttachmentOut;
  controller: AbortController;
}

type Buckets = Record<string, readonly Draft[]>;

/**
 * Вложения поля ввода: файл уходит на сервер сразу, как только выбран (`POST /attachments`), чип показывает долю
 * загрузки, ошибку и «Убрать». Сообщение уходит только с загруженными (`ready`). У каждого чата свой набор:
 * переключение чата не теряет выбранное и не путает загрузки.
 */
export function useAttachments(bucket: string) {
  const [buckets, setBuckets] = useState<Buckets>({});
  const latest = useRef<Buckets>({});
  latest.current = buckets;

  const patch = useCallback((key: string, id: string, change: Partial<Draft>) => {
    setBuckets((current) => ({
      ...current,
      [key]: (current[key] ?? []).map((item) => (item.id === id ? { ...item, ...change } : item)),
    }));
  }, []);

  const add = useCallback(
    (files: File[], _source: FileSource, threadId: string | null) => {
      const have = latest.current[bucket]?.length ?? 0;
      const room = Math.max(0, MAX_MESSAGE_ATTACHMENTS - have);
      if (files.length > room) {
        toast("Не больше десяти вложений в сообщении", {
          tone: "warning",
          description: "Лишние файлы не добавлены.",
        });
      }
      const drafts: Draft[] = [];
      for (const file of files.slice(0, room)) {
        const id = clientId();
        const kind = kindOfFile(file);
        const tooBig = file.size > MAX_ATTACHMENT_BYTES;
        drafts.push({
          id,
          name: file.name,
          mime: file.type || "application/octet-stream",
          size: file.size,
          kind,
          status: tooBig ? "failed" : "uploading",
          error: tooBig ? "Файл больше 20 МБ" : undefined,
          previewUrl: kind === "image" && !tooBig ? URL.createObjectURL(file) : undefined,
          file,
          controller: new AbortController(),
        });
      }
      if (drafts.length === 0) return;
      setBuckets((current) => ({ ...current, [bucket]: [...(current[bucket] ?? []), ...drafts] }));
      for (const draft of drafts) {
        if (draft.status === "failed") continue;
        uploadAttachment(draft.file, {
          threadId,
          signal: draft.controller.signal,
          onProgress: (progress) => patch(bucket, draft.id, { progress }),
        }).then(
          (out) => patch(bucket, draft.id, { status: "ready", progress: 1, out, kind: out.kind }),
          (error: unknown) => {
            if (draft.controller.signal.aborted) return;
            patch(bucket, draft.id, { status: "failed", error: humanMessage(error), progress: undefined });
          },
        );
      }
    },
    [bucket, patch],
  );

  const remove = useCallback(
    (id: string) => {
      const draft = latest.current[bucket]?.find((item) => item.id === id);
      if (!draft) return;
      draft.controller.abort();
      if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
      setBuckets((current) => ({
        ...current,
        [bucket]: (current[bucket] ?? []).filter((item) => item.id !== id),
      }));
      // Не отправленное вложение удаляется и на сервере; сбой удаления не важен: оно не привязано ни к одному сообщению.
      if (draft.out)
        void v3<void>("DELETE", `/api/v1/attachments/${encodeURIComponent(draft.out.id)}`).catch(() => {});
    },
    [bucket],
  );

  /** Загруженные вложения уходят с сообщением; остальное (ошибки) убирается из поля вместе с ними. */
  const take = useCallback((): OutboxAttachment[] => {
    const drafts = latest.current[bucket] ?? [];
    const ready = drafts
      .filter((item): item is Draft & { out: AttachmentOut } => item.status === "ready" && Boolean(item.out))
      .map((item) => ({
        id: item.out.id,
        name: item.out.name,
        mime: item.out.mime,
        size: item.out.size,
        kind: item.out.kind,
      }));
    for (const draft of drafts) if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
    setBuckets((current) => ({ ...current, [bucket]: [] }));
    return ready;
  }, [bucket]);

  useEffect(
    () => () => {
      for (const drafts of Object.values(latest.current)) {
        for (const draft of drafts) {
          draft.controller.abort();
          if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
        }
      }
    },
    [],
  );

  const drafts = buckets[bucket];
  const composer = useMemo<ComposerAttachment[]>(
    () => (drafts ?? []).map(({ file: _file, out: _out, controller: _controller, ...item }) => item),
    [drafts],
  );
  /** Для вьюера: открыть чип можно, пока файл ещё не отправлен. */
  const viewer = useMemo<ViewerAttachment[]>(
    () =>
      (drafts ?? [])
        .filter((item) => item.status === "ready" && item.out)
        .map((item) => ({
          id: item.id,
          name: item.name,
          mime: item.mime,
          size: item.size,
          kind: item.kind,
          url: item.out ? attachmentContentUrl(item.out.id) : (item.previewUrl ?? ""),
        })),
    [drafts],
  );

  return { items: composer, viewer, add, remove, take };
}

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
  /** Выбранный файл; у вложения, вернувшегося после отказа сервера, его нет — оно уже загружено. */
  file: File | null;
  out?: AttachmentOut;
  controller: AbortController;
}

type Buckets = Record<string, readonly Draft[]>;

const deleteUnsent = (draft: Draft) => {
  // Не отправленное вложение удаляется и на сервере; сбой удаления не важен: оно не привязано ни к одному сообщению.
  if (draft.out)
    void v3<void>("DELETE", `/api/v1/attachments/${encodeURIComponent(draft.out.id)}`).catch(() => {});
};

/**
 * Вложения поля ввода: файл уходит на сервер сразу, как только выбран (`POST /attachments`), чип показывает долю
 * загрузки, ошибку и «Убрать». Сообщение уходит только с загруженными (`ready`); чипы с ошибкой остаются в поле,
 * пока человек их не уберёт. У каждого чата свой набор: переключение чата не теряет выбранное и не путает загрузки.
 * Источник правды — ref, обновляемый сразу: быстрые подряд выбранные файлы не обходят предел в десять штук.
 */
export function useAttachments(bucket: string) {
  const store = useRef<Buckets>({});
  const [buckets, setBuckets] = useState<Buckets>({});

  const commit = useCallback((next: Buckets) => {
    store.current = next;
    setBuckets(next);
  }, []);

  const patch = useCallback(
    (key: string, id: string, change: Partial<Draft>) => {
      const list = store.current[key];
      if (!list?.some((item) => item.id === id)) return;
      commit({
        ...store.current,
        [key]: list.map((item) => (item.id === id ? { ...item, ...change } : item)),
      });
    },
    [commit],
  );

  const add = useCallback(
    (files: File[], _source: FileSource, threadId: string | null) => {
      const have = store.current[bucket]?.length ?? 0;
      const room = Math.max(0, MAX_MESSAGE_ATTACHMENTS - have);
      if (files.length > room) {
        toast("Не больше десяти вложений в сообщении", {
          tone: "warning",
          description: "Лишние файлы не добавлены.",
        });
      }
      const drafts: Draft[] = [];
      for (const file of files.slice(0, room)) {
        const kind = kindOfFile(file);
        const tooBig = file.size > MAX_ATTACHMENT_BYTES;
        drafts.push({
          id: clientId(),
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
      commit({ ...store.current, [bucket]: [...(store.current[bucket] ?? []), ...drafts] });
      for (const draft of drafts) {
        if (draft.status === "failed" || !draft.file) continue;
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
    [bucket, patch, commit],
  );

  const remove = useCallback(
    (id: string) => {
      const draft = store.current[bucket]?.find((item) => item.id === id);
      if (!draft) return;
      draft.controller.abort();
      if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
      commit({ ...store.current, [bucket]: (store.current[bucket] ?? []).filter((item) => item.id !== id) });
      deleteUnsent(draft);
    },
    [bucket, commit],
  );

  /** Загруженные вложения уходят с сообщением; чипы с ошибкой остаются в поле до «Убрать». */
  const take = useCallback((): OutboxAttachment[] => {
    const drafts = store.current[bucket] ?? [];
    const taken = drafts.filter(
      (item): item is Draft & { out: AttachmentOut } => item.status === "ready" && Boolean(item.out),
    );
    for (const draft of taken) if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
    const left = drafts.filter((item) => !taken.includes(item as Draft & { out: AttachmentOut }));
    commit({ ...store.current, [bucket]: left });
    return taken.map((item) => ({
      id: item.out.id,
      name: item.out.name,
      mime: item.out.mime,
      size: item.out.size,
      kind: item.out.kind,
    }));
  }, [bucket, commit]);

  /** Сервер не принял сообщение с вложениями: они уже загружены и возвращаются в поле того чата, откуда уходили. */
  const restore = useCallback(
    (items: readonly OutboxAttachment[], key: string) => {
      const list = store.current[key] ?? [];
      const known = new Set(list.map((item) => item.out?.id));
      const back: Draft[] = items
        .filter((item) => !known.has(item.id))
        .slice(0, Math.max(0, MAX_MESSAGE_ATTACHMENTS - list.length))
        .map((item) => ({
          ...item,
          id: clientId(),
          status: "ready" as const,
          progress: 1,
          file: null,
          controller: new AbortController(),
          out: { ...item, createdAt: "", documentId: null, ingest: "none" as const },
        }));
      if (back.length > 0) commit({ ...store.current, [key]: [...list, ...back] });
    },
    [commit],
  );

  /** Новый чат получил имя: чипы, оставшиеся в поле (с ошибкой), переезжают в поле этого чата вместе с ним. */
  const move = useCallback(
    (from: string, to: string) => {
      const left = store.current[from];
      if (!left?.length || from === to) return;
      const { [from]: _gone, ...rest } = store.current;
      commit({ ...rest, [to]: [...(rest[to] ?? []), ...left] });
    },
    [commit],
  );

  // Человек ушёл из чата: выбранные, но не отправленные вложения не остаются на сервере.
  useEffect(
    () => () => {
      for (const drafts of Object.values(store.current)) {
        for (const draft of drafts) {
          draft.controller.abort();
          if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
          deleteUnsent(draft);
        }
      }
      store.current = {};
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

  return { items: composer, viewer, add, remove, take, restore, move };
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type VoiceRecorderError = "denied" | "unsupported" | "failed";

export type VoiceRecorderState =
  | { status: "idle" }
  | { status: "recording"; elapsedMs: number }
  | { status: "error"; reason: VoiceRecorderError };

/** Форматы в порядке предпочтения: webm/opus у Chrome и Firefox, mp4 у Safari. */
const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
  "audio/ogg",
];
const TICK_MS = 250;

/** Есть ли в браузере запись звука: `MediaRecorder` и `getUserMedia` (нужен безопасный контекст). */
export function isVoiceSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    typeof navigator.mediaDevices?.getUserMedia === "function" &&
    typeof MediaRecorder !== "undefined"
  );
}

function pickMime(): string | undefined {
  if (typeof MediaRecorder.isTypeSupported !== "function") return undefined;
  return MIME_CANDIDATES.find((mime) => MediaRecorder.isTypeSupported(mime));
}

const baseType = (mime: string) => mime.split(";")[0]?.trim() || "audio/webm";

function extensionOf(mime: string): string {
  const type = baseType(mime);
  if (type === "audio/mp4") return "m4a";
  if (type === "audio/ogg") return "ogg";
  if (type === "audio/mpeg") return "mp3";
  return "webm";
}

const two = (value: number) => String(value).padStart(2, "0");

/** «Голосовое сообщение 27.10 14-05.webm»: имя файла записи по местному времени. */
export function voiceFileName(mime: string, at: Date): string {
  const day = `${two(at.getDate())}.${two(at.getMonth() + 1)}`;
  const time = `${two(at.getHours())}-${two(at.getMinutes())}`;
  return `Голосовое сообщение ${day} ${time}.${extensionOf(mime)}`;
}

/** «0:07», «12:03»: прошедшее время записи, цифры одной ширины задаёт вызывающий (`data-numeric`). */
export function formatRecordingTime(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${two(seconds % 60)}`;
}

interface Session {
  recorder: MediaRecorder;
  stream: MediaStream;
  chunks: Blob[];
  mime: string;
  startedAt: number;
  timer: number;
}

export interface VoiceRecorder {
  state: VoiceRecorderState;
  /** Браузер умеет записывать звук. */
  supported: boolean;
  /** `true` — запись пошла; `false` — не удалось (причина в `state`). */
  start: () => Promise<boolean>;
  /** Останавливает запись и отдаёт файл; `null` — записи не было или она пуста. */
  stop: () => Promise<File | null>;
  /** Бросает запись: треки остановлены, звук не сохраняется. */
  cancel: () => void;
  clearError: () => void;
}

/**
 * Запись голосового сообщения (`MediaRecorder`), без расшифровки: на выходе обычный аудиофайл. Микрофон
 * отпускается при остановке, отмене и размонтировании. Отказ в доступе даёт `error: "denied"`, а не исключение.
 */
export function useVoiceRecorder(): VoiceRecorder {
  const supported = useMemo(isVoiceSupported, []);
  const [state, setState] = useState<VoiceRecorderState>({ status: "idle" });
  const session = useRef<Session | null>(null);
  // Запрос микрофона ещё идёт, а запись отменили или экран закрыли: поток нужно сразу отпустить.
  const attempt = useRef(0);

  const release = useCallback(() => {
    const current = session.current;
    session.current = null;
    if (!current) return;
    window.clearInterval(current.timer);
    for (const track of current.stream.getTracks()) track.stop();
  }, []);

  const discard = useCallback(() => {
    attempt.current += 1;
    const current = session.current;
    if (current) {
      current.recorder.ondataavailable = null;
      current.recorder.onstop = null;
      if (current.recorder.state !== "inactive") {
        try {
          current.recorder.stop();
        } catch {
          /* запись уже остановлена */
        }
      }
    }
    release();
  }, [release]);

  const start = useCallback(async (): Promise<boolean> => {
    if (session.current) return true;
    if (!supported) {
      setState({ status: "error", reason: "unsupported" });
      return false;
    }
    const token = ++attempt.current;
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      if (token !== attempt.current) return false;
      const name = error instanceof DOMException || error instanceof Error ? error.name : "";
      const denied =
        name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError";
      setState({ status: "error", reason: denied ? "denied" : "failed" });
      return false;
    }
    if (token !== attempt.current) {
      for (const track of stream.getTracks()) track.stop();
      return false;
    }
    try {
      const mime = pickMime();
      const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      const chunks: Blob[] = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunks.push(event.data);
      };
      recorder.start();
      const startedAt = Date.now();
      const timer = window.setInterval(
        () => setState({ status: "recording", elapsedMs: Date.now() - startedAt }),
        TICK_MS,
      );
      session.current = { recorder, stream, chunks, mime: recorder.mimeType || mime || "", startedAt, timer };
      setState({ status: "recording", elapsedMs: 0 });
      return true;
    } catch {
      for (const track of stream.getTracks()) track.stop();
      setState({ status: "error", reason: "failed" });
      return false;
    }
  }, [supported]);

  const stop = useCallback((): Promise<File | null> => {
    const current = session.current;
    if (!current) return Promise.resolve(null);
    return new Promise((resolve) => {
      const finish = () => {
        const type = current.recorder.mimeType || current.mime || "audio/webm";
        const blob = new Blob(current.chunks, { type });
        release();
        setState({ status: "idle" });
        resolve(
          blob.size > 0
            ? new File([blob], voiceFileName(type, new Date(current.startedAt)), { type: baseType(type) })
            : null,
        );
      };
      current.recorder.onstop = finish;
      if (current.recorder.state === "inactive") finish();
      else {
        try {
          current.recorder.stop();
        } catch {
          finish();
        }
      }
    });
  }, [release]);

  const cancel = useCallback(() => {
    discard();
    setState({ status: "idle" });
  }, [discard]);

  const clearError = useCallback(() => {
    setState((current) => (current.status === "error" ? { status: "idle" } : current));
  }, []);

  useEffect(() => discard, [discard]);

  return { state, supported, start, stop, cancel, clearError };
}

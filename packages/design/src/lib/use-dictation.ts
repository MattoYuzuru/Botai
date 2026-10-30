import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type BandRange, bandLevels, bandRanges, type LevelMeter, peakLevel } from "./audio-levels";

/** Предел одной записи: сервер принимает не больше трёх минут, на пределе запись останавливается сама. */
export const MAX_DICTATION_MS = 180_000;

export type DictationPhase =
  | { status: "idle" }
  | { status: "recording"; elapsedMs: number }
  | { status: "transcribing" }
  | { status: "error"; message: string };

export interface TranscribeOptions {
  signal: AbortSignal;
  /** Сколько длилась запись по часам страницы: сервер списывает по ней суточный лимит. */
  durationMs: number;
}

/**
 * Превращает запись в текст. Отказ — `Error` с `message`, написанным для человека («Сервис недоступен…»): хук
 * показывает его как есть. Отмену (`signal`) хук ждёт сам и сообщения о ней не показывает.
 */
export type Transcribe = (audio: File, options: TranscribeOptions) => Promise<string>;

export const DICTATION_MESSAGES = {
  denied: "Нет доступа к микрофону. Разрешите его в настройках браузера.",
  unsupported: "Запись голоса не поддерживается в этом браузере.",
  failed: "Не удалось записать звук. Проверьте микрофон.",
  empty: "Запись получилась пустой. Попробуйте ещё раз.",
  silent: "Речь не распознана. Говорите ближе к микрофону или напишите текстом.",
  quiet: "Ничего не слышно. Проверьте микрофон и попробуйте ещё раз.",
  lost: "Микрофон отключился, запись прервана. Проверьте подключение и запишите ещё раз.",
  unknown: "Не удалось распознать речь. Напишите текстом.",
} as const;

/** Форматы в порядке предпочтения: webm/opus у Chrome и Firefox, mp4 у Safari. */
const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
  "audio/ogg",
];
const TICK_MS = 250;
/** Как часто слушаем уровень, чтобы отличить тишину от речи: короткая фраза между двумя тиками не пропадёт. */
const SAMPLE_MS = 50;
/** Уровень полосы (0…1 после отсечки шума комнаты), ниже которого на записи никто не говорил. */
export const SILENCE_LEVEL = 0.08;
/** Рекордер не прислал последний кусок звука за это время: берём то, что есть, а не ждём вечно. */
const STOP_TIMEOUT_MS = 3000;

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

/** «0:07», «12:03»: прошедшее время записи, цифры одной ширины задаёт вызывающий (`data-numeric`). */
export function formatRecordingTime(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${two(seconds % 60)}`;
}

type AudioContextCtor = typeof AudioContext;

function openAudioContext(): AudioContext | null {
  const Ctor: AudioContextCtor | undefined =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: AudioContextCtor }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    return new Ctor();
  } catch {
    return null;
  }
}

interface Metering {
  meter: LevelMeter;
  /** Самая громкая полоса за всё время записи, 0…1 (обновляется и при каждом кадре эквалайзера). */
  sample: () => number;
  /** Контекст работает: иначе анализатор молчит из-за политики автозапуска, а не из-за тишины. */
  running: () => boolean;
  disconnect: () => void;
}

/** Анализатор на том же потоке, что и запись. К динамикам он не подключён: собственный голос не звучит. */
function createMetering(context: AudioContext, stream: MediaStream): Metering | null {
  try {
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.5;
    analyser.minDecibels = -85;
    analyser.maxDecibels = -25;
    source.connect(analyser);
    const spectrum = new Uint8Array(analyser.frequencyBinCount);
    let layout: { bars: number; ranges: BandRange[] } | null = null;
    const probe = new Float32Array(16);
    const probeRanges = bandRanges(context.sampleRate, analyser.frequencyBinCount, probe.length);
    // Короткий звук (щелчок, «да») держится в окне анализатора около 20 мс: пик копится на каждом чтении,
    // а не только на редких опросах таймера.
    let loudest = 0;
    return {
      meter: {
        read(out) {
          analyser.getByteFrequencyData(spectrum);
          if (layout?.bars !== out.length) {
            layout = {
              bars: out.length,
              ranges: bandRanges(context.sampleRate, analyser.frequencyBinCount, out.length),
            };
          }
          bandLevels(spectrum, layout.ranges, out);
          loudest = Math.max(loudest, peakLevel(out));
        },
      },
      sample() {
        analyser.getByteFrequencyData(spectrum);
        bandLevels(spectrum, probeRanges, probe);
        loudest = Math.max(loudest, peakLevel(probe));
        return loudest;
      },
      running: () => context.state === "running",
      disconnect: () => {
        source.disconnect();
        analyser.disconnect();
      },
    };
  } catch {
    return null;
  }
}

interface Session {
  recorder: MediaRecorder;
  stream: MediaStream;
  context: AudioContext | null;
  metering: Metering | null;
  chunks: Blob[];
  mime: string;
  startedAt: number;
  timer: number;
  sampler: number;
  /** Самый громкий момент записи, 0…1. */
  peak: number;
}

export interface UseDictationOptions {
  /** Нет функции — диктовки нет (`supported: false`). */
  transcribe?: Transcribe;
  /** Готовый текст: вызывающий код вставляет его в поле. */
  onText: (text: string) => void;
  maxMs?: number;
}

export interface Dictation {
  phase: DictationPhase;
  /** Браузер умеет записывать звук и есть кому его распознать. */
  supported: boolean;
  /** Громкость по полосам, пока идёт запись; иначе `null`. */
  meter: LevelMeter | null;
  /** `true` — запись пошла; `false` — не удалось (причина в `phase`). */
  start: () => Promise<boolean>;
  /** «Готово»: останавливает запись и отправляет её на распознавание. */
  finish: () => Promise<void>;
  /** Бросает запись или ожидание ответа: звук не сохраняется и никуда не уходит. */
  cancel: () => void;
  clearError: () => void;
}

/**
 * Диктовка: запись голоса (`MediaRecorder`) → текст. Звук в файл и во вложения не попадает: хук отдаёт его только
 * функции `transcribe`. Микрофон и аудиоконтекст отпускаются при «Готово», отмене и размонтировании; отказ в
 * доступе даёт `error`, а не исключение. Пока идёт запись, `meter` читает спектр для эквалайзера, а на пределе
 * `maxMs` запись останавливается сама и уходит на распознавание.
 */
export function useDictation({
  transcribe,
  onText,
  maxMs = MAX_DICTATION_MS,
}: UseDictationOptions): Dictation {
  const supported = useMemo(() => isVoiceSupported() && Boolean(transcribe), [transcribe]);
  const [phase, setPhase] = useState<DictationPhase>({ status: "idle" });
  const [meter, setMeter] = useState<LevelMeter | null>(null);
  const session = useRef<Session | null>(null);
  const job = useRef<AbortController | null>(null);
  // Запрос микрофона ещё идёт, а запись отменили или экран закрыли: поток нужно сразу отпустить.
  const attempt = useRef(0);
  const latest = useRef({ transcribe, onText, maxMs });
  latest.current = { transcribe, onText, maxMs };

  const release = useCallback((current: Session) => {
    window.clearInterval(current.timer);
    window.clearInterval(current.sampler);
    current.metering?.disconnect();
    for (const track of current.stream.getTracks()) {
      track.onended = null;
      track.stop();
    }
    void current.context?.close().catch(() => {});
  }, []);

  const discard = useCallback(() => {
    attempt.current += 1;
    job.current?.abort();
    job.current = null;
    const current = session.current;
    session.current = null;
    if (!current) return;
    current.recorder.ondataavailable = null;
    current.recorder.onstop = null;
    if (current.recorder.state !== "inactive") {
      try {
        current.recorder.stop();
      } catch {
        /* запись уже остановлена */
      }
    }
    release(current);
  }, [release]);

  /** Останавливает запись, ждёт последний кусок звука и отпускает микрофон. `null` — звука нет. */
  const collect = useCallback(
    (current: Session): Promise<File | null> =>
      new Promise((resolve) => {
        let settled = false;
        const done = () => {
          if (settled) return;
          settled = true;
          window.clearTimeout(waiting);
          const type = current.recorder.mimeType || current.mime || "audio/webm";
          const blob = new Blob(current.chunks, { type });
          release(current);
          resolve(
            blob.size > 0
              ? new File([blob], `dictation.${extensionOf(type)}`, { type: baseType(type) })
              : null,
          );
        };
        // Рекордер, который не сообщил об остановке, не должен подвесить кнопку «Готово».
        const waiting = window.setTimeout(done, STOP_TIMEOUT_MS);
        current.recorder.onstop = done;
        if (current.recorder.state === "inactive") done();
        else {
          try {
            current.recorder.stop();
          } catch {
            done();
          }
        }
      }),
    [release],
  );

  const finish = useCallback(async () => {
    const current = session.current;
    const { transcribe: recognise } = latest.current;
    if (!current || !recognise) return;
    session.current = null; // с этой минуты запись принадлежит только этой функции
    window.clearInterval(current.timer);
    const durationMs = Date.now() - current.startedAt;
    const token = attempt.current;
    // Последний взгляд на уровень, до того как анализатор отпустят.
    if (current.metering) current.peak = Math.max(current.peak, current.metering.sample());
    // Нет анализатора или он не запущен — уровня мы не знаем, и тишину не угадываем.
    const heard = !current.metering?.running() || current.peak >= SILENCE_LEVEL;
    const file = await collect(current);
    setMeter(null);
    if (token !== attempt.current) return; // отменили, пока дописывался последний кусок звука
    if (!file) {
      setPhase({ status: "error", message: DICTATION_MESSAGES.empty });
      return;
    }
    if (!heard) {
      // Ни разу выше шума комнаты: на сервер такая запись не уходит (там из тишины выдумывают слова).
      setPhase({ status: "error", message: DICTATION_MESSAGES.quiet });
      return;
    }
    const controller = new AbortController();
    job.current = controller;
    setPhase({ status: "transcribing" });
    try {
      const text = (await recognise(file, { signal: controller.signal, durationMs })).trim();
      if (controller.signal.aborted) return;
      job.current = null;
      if (!text) {
        setPhase({ status: "error", message: DICTATION_MESSAGES.silent });
        return;
      }
      latest.current.onText(text);
      setPhase({ status: "idle" });
    } catch (error) {
      if (controller.signal.aborted) return;
      job.current = null;
      const message = error instanceof Error && error.message ? error.message : DICTATION_MESSAGES.unknown;
      setPhase({ status: "error", message });
    }
  }, [collect]);

  const start = useCallback(async (): Promise<boolean> => {
    if (session.current || job.current) return false;
    if (!supported) {
      setPhase({ status: "error", message: DICTATION_MESSAGES.unsupported });
      return false;
    }
    const token = ++attempt.current;
    // Контекст создаётся сразу, пока длится жест нажатия: Safari иначе оставит его «приостановленным».
    const context = openAudioContext();
    void context?.resume?.().catch(() => {});
    const closeContext = () => void context?.close().catch(() => {});
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      closeContext();
      if (token !== attempt.current) return false;
      const name = error instanceof DOMException || error instanceof Error ? error.name : "";
      const denied =
        name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError";
      setPhase({ status: "error", message: denied ? DICTATION_MESSAGES.denied : DICTATION_MESSAGES.failed });
      return false;
    }
    if (token !== attempt.current) {
      for (const track of stream.getTracks()) track.stop();
      closeContext();
      return false;
    }
    try {
      const mime = pickMime();
      const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      const chunks: Blob[] = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunks.push(event.data);
      };
      const metering = context ? createMetering(context, stream) : null;
      recorder.start();
      const startedAt = Date.now();
      const timer = window.setInterval(() => {
        const elapsedMs = Date.now() - startedAt;
        if (elapsedMs >= latest.current.maxMs) void finish();
        else setPhase({ status: "recording", elapsedMs });
      }, TICK_MS);
      const current: Session = {
        recorder,
        stream,
        context,
        metering,
        chunks,
        mime: recorder.mimeType || mime || "",
        startedAt,
        timer,
        sampler: window.setInterval(() => {
          if (current.metering) current.peak = Math.max(current.peak, current.metering.sample());
        }, SAMPLE_MS),
        peak: metering?.sample() ?? 0,
      };
      session.current = current;
      // Микрофон отключили (вынули гарнитуру, отозвали разрешение): запись прервана, это не «Готово».
      for (const track of stream.getTracks()) {
        track.onended = () => {
          if (session.current !== current) return;
          discard();
          setMeter(null);
          setPhase({ status: "error", message: DICTATION_MESSAGES.lost });
        };
      }
      setMeter(metering?.meter ?? null);
      setPhase({ status: "recording", elapsedMs: 0 });
      return true;
    } catch {
      for (const track of stream.getTracks()) track.stop();
      closeContext();
      setPhase({ status: "error", message: DICTATION_MESSAGES.failed });
      return false;
    }
  }, [supported, finish, discard]);

  const cancel = useCallback(() => {
    discard();
    setMeter(null);
    setPhase({ status: "idle" });
  }, [discard]);

  const clearError = useCallback(() => {
    setPhase((current) => (current.status === "error" ? { status: "idle" } : current));
  }, []);

  useEffect(() => discard, [discard]);

  return { phase, supported, meter, start, finish, cancel, clearError };
}

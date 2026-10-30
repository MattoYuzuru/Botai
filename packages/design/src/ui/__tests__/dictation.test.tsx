import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NBSP } from "../../lib/format";
import { motionPreference } from "../../lib/preferences";
import type { Transcribe } from "../../lib/use-dictation";
import { ChatComposer } from "../chat-composer/chat-composer";

const plain = (text: string | null) => (text ?? "").replaceAll(NBSP, " ");

class FakeRecorder {
  static isTypeSupported = (type: string) => type === "audio/webm;codecs=opus";
  /** Рекордер, который не сообщает об остановке: `onstop` не приходит никогда. */
  static mute = false;
  state: "inactive" | "recording" = "inactive";
  mimeType: string;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  constructor(
    readonly stream: unknown,
    options?: { mimeType?: string },
  ) {
    this.mimeType = options?.mimeType ?? "";
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["voice"], { type: this.mimeType }) });
    if (!FakeRecorder.mute) queueMicrotask(() => this.onstop?.());
  }
}

/** Громкость, которую «слышит» анализатор: тест меняет `volume.value` (0…255 на каждый бин спектра). */
const volume = { value: 0 };
const contextState = { value: "running" };
const analyser = {
  fftSize: 0,
  frequencyBinCount: 512,
  smoothingTimeConstant: 0,
  minDecibels: 0,
  maxDecibels: 0,
  connect: vi.fn(),
  disconnect: vi.fn(),
  getByteFrequencyData: (data: Uint8Array) => data.fill(volume.value),
};
const sourceNode = { connect: vi.fn(), disconnect: vi.fn() };
const contexts: FakeAudioContext[] = [];
class FakeAudioContext {
  sampleRate = 48_000;
  state = contextState.value;
  close = vi.fn().mockResolvedValue(undefined);
  resume = vi.fn().mockResolvedValue(undefined);
  createMediaStreamSource = vi.fn(() => sourceNode);
  createAnalyser = vi.fn(() => analyser);
  constructor() {
    contexts.push(this);
  }
}

/** Кадры анимации вручную: тест сам решает, когда «прошло 16 мс». */
let frames: FrameRequestCallback[] = [];
let clock = 0;
function paint(count = 1, step = 16) {
  for (let index = 0; index < count; index += 1) {
    clock += step;
    const due = frames;
    frames = [];
    act(() => {
      for (const callback of due) callback(clock);
    });
  }
}

const track = { stop: vi.fn(), onended: null as (() => void) | null };
let getUserMedia: ReturnType<typeof vi.fn>;
const original = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");

beforeEach(() => {
  frames = [];
  clock = 0;
  volume.value = 120; // кто-то говорит; тесты про тишину обнуляют
  contextState.value = "running";
  FakeRecorder.mute = false;
  contexts.length = 0;
  track.stop.mockClear();
  track.onended = null;
  getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [track] });
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal("cancelAnimationFrame", () => {
    frames = [];
  });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  motionPreference.write("system");
  if (original) Object.defineProperty(navigator, "mediaDevices", original);
  else Reflect.deleteProperty(navigator, "mediaDevices");
});

function Harness({
  initial = "",
  onTranscribe,
  onSend = () => {},
  resetKey,
}: {
  initial?: string;
  onTranscribe?: Transcribe;
  onSend?: (text: string) => void;
  resetKey?: string;
}) {
  const [value, setValue] = useState(initial);
  return (
    <ChatComposer
      value={value}
      onValueChange={setValue}
      onSend={onSend}
      onTranscribe={onTranscribe}
      resetKey={resetKey}
    />
  );
}

const mic = () => screen.getByRole("button", { name: "Надиктовать текст" });
const field = () => screen.getByRole("textbox", { name: "Сообщение агенту" });
const bars = () => Array.from(document.querySelectorAll<HTMLElement>("[data-bar]"));
const scale = (node: HTMLElement) =>
  Number(/scaleY\(([\d.]+)\)/.exec(node.style.transform)?.[1] ?? Number.NaN);
const peak = () => Math.max(...bars().map(scale));

async function startRecording() {
  await userEvent.click(mic());
  await screen.findByRole("button", { name: "Готово" });
}

describe("диктовка: микрофон в поле ввода", () => {
  it("кнопка микрофона есть, только если есть кому распознавать и браузер умеет записывать", () => {
    const { unmount } = render(<Harness onTranscribe={async () => "текст"} />);
    expect(mic()).toBeInTheDocument();
    unmount();
    render(<Harness />);
    expect(screen.queryByRole("button", { name: "Надиктовать текст" })).toBeNull();
  });

  it("браузер без MediaRecorder: кнопки нет", () => {
    vi.stubGlobal("MediaRecorder", undefined);
    render(<Harness onTranscribe={async () => "текст"} />);
    expect(screen.queryByRole("button", { name: "Надиктовать текст" })).toBeNull();
  });

  it("пока поле пустое, остаётся один микрофон; с текстом рядом появляется «Отправить»", async () => {
    render(<Harness onTranscribe={async () => "текст"} />);
    expect(screen.queryByRole("button", { name: "Отправить" })).toBeNull();
    await userEvent.type(field(), "а");
    expect(mic()).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled();
  });
});

describe("диктовка: запись и эквалайзер", () => {
  it("вместо поля — эквалайзер, таймер, «Отменить запись» и «Готово»; надписи «Идёт запись» на экране нет", async () => {
    render(<Harness onTranscribe={async () => "текст"} />);
    await startRecording();
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    const equalizer = document.querySelector("[data-equalizer='bars']");
    expect(equalizer).not.toBeNull();
    expect(bars().length).toBeGreaterThanOrEqual(24);
    expect(bars().length).toBeLessThanOrEqual(32);
    expect(document.querySelector("[data-recording-time]")).toHaveTextContent("0:00");
    expect(screen.getByRole("button", { name: "Отменить запись" })).toBeInTheDocument();
    // Для скринридера состояние названо словами, на экране его заменил эквалайзер.
    expect(screen.getByText("Идёт запись")).toHaveClass("sr-only");
    expect(equalizer).toHaveAttribute("aria-hidden", "true");
    // Поле спрятано (CSS в тестах не грузится, поэтому смотрим на класс), его значение остаётся.
    expect(field().parentElement).toHaveClass("hidden");
  });

  it("высота столбиков следует за громкостью в реальном времени: тишина — точки, голос — высокие, спад плавный", async () => {
    volume.value = 0;
    render(<Harness onTranscribe={async () => "текст"} />);
    await startRecording();
    paint(3);
    const resting = peak();
    expect(resting).toBeLessThan(0.2);
    expect(bars().every((node) => scale(node) > 0)).toBe(true); // линия по центру видна и в тишине

    volume.value = 190;
    paint(8);
    const speaking = peak();
    expect(speaking).toBeGreaterThan(0.7);

    volume.value = 90;
    paint(8);
    const quieter = peak();
    expect(quieter).toBeLessThan(speaking);
    expect(quieter).toBeGreaterThan(resting);

    volume.value = 0;
    paint(1);
    const falling = peak();
    expect(falling).toBeLessThan(quieter);
    expect(falling).toBeGreaterThan(resting); // спад не мгновенный
    paint(60);
    expect(peak()).toBeLessThan(0.2);
  });

  it("ширина диктует число столбиков, не меньше 12 и не больше 32", async () => {
    const width = (value: number) =>
      vi
        .spyOn(HTMLElement.prototype, "getBoundingClientRect")
        .mockReturnValue({ width: value, height: 32 } as DOMRect);
    width(236);
    const { unmount } = render(<Harness onTranscribe={async () => "текст"} />);
    await startRecording();
    expect(bars()).toHaveLength(30);
    unmount();
    width(60);
    render(<Harness onTranscribe={async () => "текст"} />);
    await startRecording();
    expect(bars()).toHaveLength(12);
  });

  it("при сокращённом движении вместо столбиков — статичная полоса уровня", async () => {
    motionPreference.write("calm");
    render(<Harness onTranscribe={async () => "текст"} />);
    await startRecording();
    expect(document.querySelector("[data-equalizer='calm']")).not.toBeNull();
    expect(bars()).toHaveLength(0);
    expect(frames).toHaveLength(0); // ни одного кадра анимации не запрошено
  });

  it("время записи идёт и показано цифрами одной ширины", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    try {
      render(<Harness onTranscribe={async () => "текст"} />);
      fireEvent.click(mic());
      await act(async () => {
        await Promise.resolve();
      });
      await act(async () => {
        vi.advanceTimersByTime(7_000);
      });
      const time = document.querySelector("[data-recording-time]");
      expect(plain(time?.textContent ?? null)).toBe("0:07");
      expect(time).toHaveAttribute("data-numeric");
    } finally {
      vi.useRealTimers();
    }
  });

  it("на пределе в три минуты запись останавливается сама и уходит на распознавание", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    try {
      const transcribe = vi.fn<Transcribe>().mockResolvedValue("сказано");
      render(<Harness onTranscribe={transcribe} />);
      fireEvent.click(mic());
      await act(async () => {
        await Promise.resolve();
      });
      await act(async () => {
        vi.advanceTimersByTime(179_000);
      });
      expect(transcribe).not.toHaveBeenCalled();
      await act(async () => {
        vi.advanceTimersByTime(1_000);
        await Promise.resolve();
        await Promise.resolve();
      });
      await vi.waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1));
      expect(transcribe.mock.calls[0]?.[1].durationMs).toBeGreaterThanOrEqual(180_000);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("диктовка: «Готово» и отмена", () => {
  it("«Готово»: запись уходит на распознавание, текст встаёт в пустое поле, фокус в поле, сообщение не отправляется", async () => {
    const onSend = vi.fn();
    let resolve: (text: string) => void = () => {};
    const transcribe = vi.fn<Transcribe>(
      () =>
        new Promise<string>((done) => {
          resolve = done;
        }),
    );
    render(<Harness onTranscribe={transcribe} onSend={onSend} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));

    // Пока идёт распознавание: «Распознаю…», кнопка занята, микрофон уже отпущен.
    expect(await screen.findByText("Распознаю…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Готово" })).toHaveAttribute("aria-busy", "true");
    expect(track.stop).toHaveBeenCalled();
    expect(contexts[0]?.close).toHaveBeenCalled();
    const [audio, options] = transcribe.mock.calls[0] ?? [];
    expect(audio).toBeInstanceOf(File);
    expect(audio?.type).toBe("audio/webm");
    expect(audio?.size).toBeGreaterThan(0);
    expect(audio?.name).toBe("dictation.webm");
    expect(options?.signal.aborted).toBe(false);
    expect(options?.durationMs).toBeGreaterThanOrEqual(0);

    await act(async () => resolve("Какие у меня пары завтра"));
    await waitFor(() => expect(field()).toHaveValue("Какие у меня пары завтра"));
    expect(field().parentElement).not.toHaveClass("hidden");
    expect(field()).toHaveFocus();
    expect(screen.queryByText("Распознаю…")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(onSend).not.toHaveBeenCalled();
    // Человек поправляет текст и отправляет сам.
    expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled();
  });

  it("если в поле уже был текст, распознанное добавляется через пробел", async () => {
    render(<Harness initial={"Привет, \n"} onTranscribe={async () => "какие пары завтра"} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(field()).toHaveValue("Привет, какие пары завтра"));
    expect((field() as HTMLTextAreaElement).selectionStart).toBe("Привет, какие пары завтра".length);
  });

  it("«Отменить запись» ничего не делает: нет распознавания, поле как было, микрофон отпущен", async () => {
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("не должно появиться");
    render(<Harness initial="черновик" onTranscribe={transcribe} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Отменить запись" }));
    expect(transcribe).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
    expect(field()).toHaveValue("черновик");
    expect(field().parentElement).not.toHaveClass("hidden");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(document.querySelector("[data-recording]")).toBeNull();
  });

  it("отмена во время распознавания обрывает запрос и не вставляет текст", async () => {
    let signal: AbortSignal | undefined;
    let resolve: (text: string) => void = () => {};
    const transcribe = vi.fn<Transcribe>((_audio, options) => {
      signal = options.signal;
      return new Promise<string>((done) => {
        resolve = done;
      });
    });
    render(<Harness onTranscribe={transcribe} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await userEvent.click(await screen.findByRole("button", { name: "Отменить распознавание" }));
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve("поздний ответ"));
    expect(field()).toHaveValue("");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("смена resetKey (другой чат) бросает запись: текст не попадёт в поле чужого чата", async () => {
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("чужой текст");
    const { rerender } = render(<Harness onTranscribe={transcribe} resetKey="t1" />);
    await startRecording();
    rerender(<Harness onTranscribe={transcribe} resetKey="t2" />);
    await waitFor(() => expect(document.querySelector("[data-recording]")).toBeNull());
    expect(transcribe).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
  });
});

describe("диктовка: ошибки словами, без alert", () => {
  it("отказ в доступе к микрофону: сообщение в role=alert, запись не началась, «Закрыть» убирает его", async () => {
    getUserMedia.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    render(<Harness onTranscribe={async () => "текст"} />);
    await userEvent.click(mic());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Нет доступа к микрофону. Разрешите его в настройках браузера.");
    expect(document.querySelector("[data-recording]")).toBeNull();
    expect(contexts[0]?.close).toHaveBeenCalled(); // аудиоконтекст не повис
    await userEvent.click(within(alert).getByRole("button", { name: "Закрыть" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("нет микрофона вообще — другой текст", async () => {
    getUserMedia.mockRejectedValue(new DOMException("none", "NotFoundError"));
    render(<Harness onTranscribe={async () => "текст"} />);
    await userEvent.click(mic());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не удалось записать звук. Проверьте микрофон.",
    );
  });

  it("сервис недоступен или лимит: текст ошибки от функции показан как есть, поле не тронуто", async () => {
    const transcribe = vi
      .fn<Transcribe>()
      .mockRejectedValue(new Error("Распознавание речи сейчас недоступно. Напишите текстом."));
    render(<Harness initial="набрано" onTranscribe={transcribe} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Распознавание речи сейчас недоступно. Напишите текстом.",
    );
    expect(field()).toHaveValue("набрано");
    expect(field().parentElement).not.toHaveClass("hidden");
    expect(mic()).toBeEnabled(); // можно записать ещё раз
  });

  it("ошибка без понятного текста заменяется общим", async () => {
    render(<Harness onTranscribe={vi.fn<Transcribe>().mockRejectedValue("сломалось")} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не удалось распознать речь. Напишите текстом.",
    );
  });

  it("ничего не расслышано: пустой ответ — сообщение, а не пустая вставка", async () => {
    render(<Harness initial="а" onTranscribe={async () => "  "} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Речь не распознана");
    expect(field()).toHaveValue("а");
  });
});

describe("диктовка: тишина не уходит на сервер", () => {
  it("запись, на которой голос ни разу не поднялся над шумом комнаты, не отправляется", async () => {
    volume.value = 0;
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("выдумка из тишины");
    render(<Harness initial="набрано" onTranscribe={transcribe} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Ничего не слышно");
    expect(transcribe).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled(); // микрофон отпущен
    expect(field()).toHaveValue("набрано");
    expect(mic()).toBeEnabled();
  });

  it("достаточно одного громкого мгновения за всю запись: потом можно замолчать", async () => {
    volume.value = 150;
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("сказано");
    render(<Harness onTranscribe={transcribe} />);
    await startRecording();
    volume.value = 0;
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(field()).toHaveValue("сказано"));
    expect(transcribe).toHaveBeenCalledTimes(1);
  });

  it("короткий звук, который увидел один кадр эквалайзера, засчитывается как голос", async () => {
    volume.value = 0;
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("да");
    render(<Harness onTranscribe={transcribe} />);
    await startRecording();
    volume.value = 200;
    paint(1); // один кадр: щелчок или короткое «да» длится десятки миллисекунд
    volume.value = 0;
    paint(1);
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1));
  });

  it("слышно только тихо, но выше шума: запись уходит", async () => {
    volume.value = 45; // около -75 дБ: шёпот издалека
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("шёпотом");
    render(<Harness onTranscribe={transcribe} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(field()).toHaveValue("шёпотом"));
  });

  it("приостановленный аудиоконтекст молчит из-за политики браузера, а не из-за тишины: запись уходит", async () => {
    volume.value = 0;
    contextState.value = "suspended";
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("сказано");
    render(<Harness onTranscribe={transcribe} />);
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1));
  });

  it("браузер без AudioContext: уровня не знаем, запись уходит", async () => {
    vi.stubGlobal("AudioContext", undefined);
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("сказано");
    render(<Harness onTranscribe={transcribe} />);
    await startRecording();
    expect(document.querySelector("[data-equalizer='bars']")).not.toBeNull(); // столбики лежат точками
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1));
  });
});

describe("диктовка: жест, фокус, зависший рекордер, отключённый микрофон", () => {
  it("аудиоконтекст запускается синхронно в жесте нажатия, не дожидаясь разрешения на микрофон (iOS)", async () => {
    let grant: (stream: unknown) => void = () => {};
    getUserMedia.mockReturnValue(
      new Promise((resolve) => {
        grant = resolve;
      }),
    );
    render(<Harness onTranscribe={async () => "текст"} />);
    fireEvent.click(mic());
    expect(contexts).toHaveLength(1);
    expect(contexts[0]?.resume).toHaveBeenCalledTimes(1); // разрешение ещё не получено
    expect(document.querySelector("[data-recording]")).toBeNull();
    await act(async () => grant({ getTracks: () => [track] }));
    await screen.findByRole("button", { name: "Готово" });
    expect(contexts[0]?.resume).toHaveBeenCalledTimes(1);
  });

  it("фокус: запись началась — на «Готово»; отмена, ошибка и вставка возвращают его в поле", async () => {
    const transcribe = vi
      .fn<Transcribe>()
      .mockRejectedValueOnce(new Error("Сервис недоступен"))
      .mockResolvedValue("сказано");
    render(<Harness onTranscribe={transcribe} />);

    await startRecording();
    expect(screen.getByRole("button", { name: "Готово" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Отменить запись" }));
    expect(field()).toHaveFocus();

    await startRecording();
    expect(screen.getByRole("button", { name: "Готово" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await screen.findByRole("alert");
    expect(field()).toHaveFocus();

    (document.activeElement as HTMLElement).blur();
    await startRecording();
    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(field()).toHaveValue("сказано"));
    expect(field()).toHaveFocus();
  });

  it("если рекордер не прислал последний кусок, через 3 секунды берётся то, что есть", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
    try {
      FakeRecorder.mute = true;
      const transcribe = vi.fn<Transcribe>().mockResolvedValue("сказано");
      render(<Harness onTranscribe={transcribe} />);
      fireEvent.click(mic());
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: "Готово" }));
      await act(async () => {
        vi.advanceTimersByTime(2_900);
      });
      expect(transcribe).not.toHaveBeenCalled(); // ещё ждём
      await act(async () => {
        vi.advanceTimersByTime(200);
        await Promise.resolve();
        await Promise.resolve();
      });
      await vi.waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1));
      expect(track.stop).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("микрофон отключили посреди записи: запись прервана, понятная ошибка, на сервер ничего не уходит", async () => {
    const transcribe = vi.fn<Transcribe>().mockResolvedValue("не должно появиться");
    render(<Harness initial="набрано" onTranscribe={transcribe} />);
    await startRecording();
    act(() => track.onended?.());
    expect(await screen.findByRole("alert")).toHaveTextContent("Микрофон отключился");
    expect(document.querySelector("[data-recording]")).toBeNull();
    expect(transcribe).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
    expect(field()).toHaveValue("набрано");
  });

  it("эквалайзер с первого кадра точки, а не полная высота", async () => {
    render(<Harness onTranscribe={async () => "текст"} />);
    await startRecording();
    expect(frames.length).toBeGreaterThan(0); // кадр ещё не нарисован
    expect(bars().length).toBeGreaterThan(0);
    for (const bar of bars()) expect(bar.style.transform).toBe("scaleY(0.120)");
  });
});

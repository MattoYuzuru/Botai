import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MOCK_DICTATION } from "@/mocks/chat-handlers";
import { ask, chatHarness, composer, openNewChat, wait } from "@/test/chat-harness";

const harness = chatHarness();

const file = (name: string, type: string, body = "data") => new File([body], name, { type });
const fileInput = () => document.querySelector<HTMLInputElement>("[data-composer-file]") as HTMLInputElement;
const chips = () => screen.queryByRole("list", { name: "Вложения" });
const timeline = () => document.querySelector("[data-timeline]");
const sendButton = () => screen.getByRole("button", { name: "Отправить" });

const original = {
  create: URL.createObjectURL,
  revoke: URL.revokeObjectURL,
  mediaDevices: Object.getOwnPropertyDescriptor(navigator, "mediaDevices"),
};
beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  URL.createObjectURL = original.create;
  URL.revokeObjectURL = original.revoke;
  if (original.mediaDevices) Object.defineProperty(navigator, "mediaDevices", original.mediaDevices);
  else Reflect.deleteProperty(navigator, "mediaDevices");
});

describe("вложения: «+», чипы, отправка", () => {
  it("файл грузится сразу: чип «готово»; отправка без текста, вложение видно в сообщении и открывается во вьюере", async () => {
    const { user } = await openNewChat();
    await user.upload(fileInput(), file("доска.png", "image/png"));
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    await waitFor(
      () => expect(within(list).getByRole("button", { name: "Открыть доска.png" })).toBeEnabled(),
      wait,
    );
    // Текста нет, но вложение готово — отправить можно.
    expect(sendButton()).toBeEnabled();
    await user.click(sendButton());

    // В ленте — миниатюра сообщения человека, поле очистилось, агент ответил про изображение честно.
    const image = await screen.findByRole("img", { name: "доска.png" }, wait);
    expect(image).toHaveAttribute("src", expect.stringMatching(/^\/api\/v1\/attachments\/.+\/content$/));
    expect(await screen.findByText(/Изображение «доска\.png» получено/, undefined, wait)).toBeInTheDocument();
    expect(chips()).toBeNull();

    // Вьюер: лайтбокс картинки; Esc закрывает.
    await user.click(screen.getByRole("button", { name: "Открыть доска.png" }));
    const viewer = await screen.findByRole("dialog");
    expect(within(viewer).getByRole("img", { name: "доска.png" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("чип можно убрать до отправки; убранное не уходит", async () => {
    const { user } = await openNewChat();
    await user.upload(fileInput(), file("лишнее.pdf", "application/pdf"));
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    await waitFor(
      () => expect(within(list).getByRole("button", { name: "Открыть лишнее.pdf" })).toBeEnabled(),
      wait,
    );
    await user.click(within(list).getByRole("button", { name: "Убрать лишнее.pdf" }));
    await waitFor(() => expect(chips()).toBeNull());
    expect(sendButton()).toBeDisabled();
  });

  it("PDF: сообщение несёт документ, открывается во встроенном просмотре", async () => {
    const { user } = await openNewChat();
    await user.upload(fileInput(), file("лекция.pdf", "application/pdf"));
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    await waitFor(
      () => expect(within(list).getByRole("button", { name: "Открыть лекция.pdf" })).toBeEnabled(),
      wait,
    );
    await user.type(composer(), "О чём документ?");
    await user.click(sendButton());
    expect(await screen.findByText(/Документ «лекция\.pdf» получен/, undefined, wait)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Открыть лекция.pdf" }, wait));
    const viewer = await screen.findByRole("dialog");
    expect(within(viewer).getByTitle("лекция.pdf")).toHaveAttribute(
      "src",
      expect.stringMatching(/\/api\/v1\/attachments\/.+\/content$/),
    );
  });

  it("неподдерживаемый файл: чип с ошибкой сервера человеческим языком, отправка не открывается", async () => {
    const { user } = await openNewChat();
    await user.upload(fileInput(), file("установщик.exe", "application/x-msdownload"));
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    expect(
      await within(list).findByText(/Такой тип файла не поддерживается/, undefined, wait),
    ).toBeInTheDocument();
    expect(sendButton()).toBeDisabled();
    await user.click(within(list).getByRole("button", { name: "Убрать установщик.exe" }));
    await waitFor(() => expect(chips()).toBeNull());
  });

  it("файл больше 20 МБ не уходит на сервер: ошибка на чипе сразу", async () => {
    const { user } = await openNewChat();
    const big = file("запись.mov", "video/quicktime");
    Object.defineProperty(big, "size", { value: 21 * 1024 * 1024 });
    await user.upload(fileInput(), big);
    expect(await screen.findByText("Файл больше 20 МБ", undefined, wait)).toBeInTheDocument();
    expect(sendButton()).toBeDisabled();
  });

  it("сервер не принял сообщение с вложениями (422): тост, чипы остаются для повторной отправки, в ленте их нет", async () => {
    harness.server.use(
      http.post("*/api/v1/chat/threads/:threadId/messages", () =>
        HttpResponse.json(
          { code: "validation", title: "validation", status: 422, detail: "Вложения не приняты" },
          { status: 422, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );
    const { user } = await openNewChat();
    await user.upload(fileInput(), file("доска.png", "image/png"));
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    await waitFor(
      () => expect(within(list).getByRole("button", { name: "Открыть доска.png" })).toBeEnabled(),
      wait,
    );
    await user.click(sendButton());
    expect(await screen.findByText("Не удалось отправить вложения", undefined, wait)).toBeInTheDocument();
    // Чип вернулся: отправить можно снова; оптимистичного вложения в ленте нет.
    const back = await screen.findByRole("list", { name: "Вложения" }, wait);
    expect(within(back).getByRole("button", { name: "Открыть доска.png" })).toBeEnabled();
    expect(sendButton()).toBeEnabled();
    expect(timeline()).toBeNull();
  });

  it("чип с ошибкой не пропадает при отправке: остаётся в поле, пока человек его не уберёт", async () => {
    const { user } = await openNewChat();
    await user.upload(fileInput(), file("хорошее.pdf", "application/pdf"));
    await user.upload(fileInput(), file("установщик.exe", "application/x-msdownload"));
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    await within(list).findByText(/Такой тип файла не поддерживается/, undefined, wait);
    await waitFor(
      () => expect(within(list).getByRole("button", { name: "Открыть хорошее.pdf" })).toBeEnabled(),
      wait,
    );
    await user.type(composer(), "Вот документ");
    await user.click(sendButton());
    expect(await screen.findByText(/Документ «хорошее\.pdf» получен/, undefined, wait)).toBeInTheDocument();
    const left = screen.getByRole("list", { name: "Вложения" });
    expect(within(left).queryByText("хорошее.pdf")).toBeNull();
    expect(within(left).getByText("установщик.exe")).toBeInTheDocument();
  });

  it("перетаскивание на область чата и вставка изображения из буфера добавляют вложения", async () => {
    await openNewChat();
    const zone = document.querySelector("[data-drop-zone]") as HTMLElement;
    const dropped = file("конспект.pdf", "application/pdf");
    fireEvent.dragEnter(zone, { dataTransfer: { types: ["Files"] } });
    expect(screen.getByText("Отпустите файлы, чтобы прикрепить")).toBeInTheDocument();
    fireEvent.drop(zone, { dataTransfer: { types: ["Files"], files: [dropped] } });
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    expect(within(list).getByText("конспект.pdf")).toBeInTheDocument();

    const pasted = file("скриншот.png", "image/png");
    fireEvent.paste(composer(), { clipboardData: { files: [pasted], types: ["Files"] } });
    await waitFor(() => expect(within(list).getByText("скриншот.png")).toBeInTheDocument(), wait);
  });
});

describe("аудиофайл через «+» остаётся вложением", () => {
  it("файл не распознаётся: агент честно говорит об этом, запись проигрывается во вьюере", async () => {
    const { user } = await openNewChat();
    await user.upload(fileInput(), file("лекция.webm", "audio/webm"));
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    await waitFor(
      () => expect(within(list).getByRole("button", { name: "Открыть лекция.webm" })).toBeEnabled(),
      wait,
    );
    await user.click(sendButton());
    expect(await screen.findByText(/расшифровки пока нет/, undefined, wait)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Открыть лекция.webm" }, wait));
    const viewer = await screen.findByRole("dialog");
    expect(viewer.querySelector("audio")).not.toBeNull();
  });
});

describe("диктовка: речь в текст в поле ввода", () => {
  class FakeRecorder {
    static isTypeSupported = (type: string) => type === "audio/webm;codecs=opus";
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
      queueMicrotask(() => this.onstop?.());
    }
  }

  function mockMicrophone() {
    const track = { stop: vi.fn() };
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) },
    });
    return track;
  }

  const mic = () => screen.getByRole("button", { name: "Надиктовать текст" });

  it("запись → «Готово» → текст в поле, сообщение не отправлено; человек правит и отправляет сам", async () => {
    const track = mockMicrophone();
    const { user } = await openNewChat();
    await user.click(mic());
    expect(await screen.findByRole("button", { name: "Отменить запись" })).toBeInTheDocument();
    expect(document.querySelector("[data-equalizer]")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Готово" }));

    await waitFor(() => expect(composer()).toHaveValue(MOCK_DICTATION), wait);
    expect(composer()).toHaveFocus();
    expect(track.stop).toHaveBeenCalled();
    // Ни вложения, ни сообщения: звук ушёл только на распознавание.
    expect(chips()).toBeNull();
    expect(screen.queryByText(MOCK_DICTATION, { selector: "[data-timeline] *" })).toBeNull();

    await user.type(composer(), " и послезавтра");
    await user.click(sendButton());
    await waitFor(
      () =>
        expect(
          within(timeline() as HTMLElement).getByText(`${MOCK_DICTATION} и послезавтра`),
        ).toBeInTheDocument(),
      wait,
    );
  });

  it("отмена ничего не отправляет на сервер и не меняет поле", async () => {
    const track = mockMicrophone();
    const requests = vi.fn();
    harness.server.use(
      http.post("*/api/v1/speech/transcribe", () => {
        requests();
        return HttpResponse.json({ text: "не должно появиться" });
      }),
    );
    const { user } = await openNewChat();
    await user.type(composer(), "черновик");
    await user.click(mic());
    await user.click(await screen.findByRole("button", { name: "Отменить запись" }));
    expect(track.stop).toHaveBeenCalled();
    expect(composer()).toHaveValue("черновик");
    expect(requests).not.toHaveBeenCalled();
  });

  it("сервис выключен, лимит исчерпан: человеческий текст в alert, поле не тронуто", async () => {
    mockMicrophone();
    harness.server.use(
      http.post("*/api/v1/speech/transcribe", () =>
        HttpResponse.json(
          {
            type: "https://botai.mnema.app/errors/stt_unavailable",
            title: "stt_unavailable",
            status: 503,
            code: "stt_unavailable",
            detail: "Распознавание речи сейчас не работает. Напишите текстом.",
          },
          { status: 503, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );
    const { user } = await openNewChat();
    await user.click(mic());
    await user.click(await screen.findByRole("button", { name: "Готово" }));
    expect(await screen.findByRole("alert", undefined, wait)).toHaveTextContent(
      "Распознавание речи сейчас не работает. Напишите текстом.",
    );
    expect(composer()).toHaveValue("");

    harness.server.use(
      http.post("*/api/v1/speech/transcribe", () =>
        HttpResponse.json(
          {
            type: "https://botai.mnema.app/errors/quota_exceeded",
            title: "quota_exceeded",
            status: 429,
            code: "quota_exceeded",
            detail: "Слишком много записей за час. Попробуйте через 12 мин или напишите текстом.",
            scope: "speech_hour",
          },
          { status: 429, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Закрыть" }));
    await user.click(mic());
    await user.click(await screen.findByRole("button", { name: "Готово" }));
    expect(await screen.findByRole("alert", undefined, wait)).toHaveTextContent("Попробуйте через 12 мин");
  });

  it("отказ в микрофоне — сообщение словами, без alert браузера", async () => {
    mockMicrophone();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi
          .fn()
          .mockRejectedValue(Object.assign(new Error("denied"), { name: "NotAllowedError" })),
      },
    });
    const { user } = await openNewChat();
    await user.click(mic());
    expect(await screen.findByRole("alert")).toHaveTextContent(/Нет доступа к микрофону/);
  });

  it("браузер без MediaRecorder: кнопки микрофона нет", async () => {
    vi.stubGlobal("MediaRecorder", undefined);
    await openNewChat();
    expect(screen.queryByRole("button", { name: "Надиктовать текст" })).toBeNull();
    expect(userEvent).toBeDefined();
    void ask;
  });
});

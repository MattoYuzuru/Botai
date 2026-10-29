import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ask, chatHarness, composer, openNewChat, wait } from "@/test/chat-harness";

chatHarness();

const file = (name: string, type: string, body = "data") => new File([body], name, { type });
const fileInput = () => document.querySelector<HTMLInputElement>("[data-composer-file]") as HTMLInputElement;
const chips = () => screen.queryByRole("list", { name: "Вложения" });
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

describe("голосовое сообщение: запись MediaRecorder без расшифровки", () => {
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

  it("запись → чип → отправка; агент честно говорит, что расшифровки нет; запись проигрывается во вьюере", async () => {
    const track = mockMicrophone();
    const { user } = await openNewChat();
    await user.click(screen.getByRole("button", { name: "Записать голосовое сообщение" }));
    expect(await screen.findByText(/Идёт запись/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Остановить и прикрепить" }));
    const list = await screen.findByRole("list", { name: "Вложения" }, wait);
    const name = /Голосовое сообщение \d\d\.\d\d \d\d-\d\d\.webm/;
    await waitFor(
      () => expect(within(list).getByRole("button", { name: /^Открыть Голосовое/ })).toBeEnabled(),
      wait,
    );
    expect(within(list).getByText(name)).toBeInTheDocument();
    expect(track.stop).toHaveBeenCalled();
    await user.click(sendButton());
    expect(await screen.findByText(/расшифровки пока нет/, undefined, wait)).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: /^Открыть Голосовое/ }, wait));
    const viewer = await screen.findByRole("dialog");
    expect(viewer.querySelector("audio")).not.toBeNull();
  });

  it("отмена записи ничего не прикрепляет; отказ в микрофоне — сообщение словами, без alert", async () => {
    const track = mockMicrophone();
    const { user } = await openNewChat();
    await user.click(screen.getByRole("button", { name: "Записать голосовое сообщение" }));
    await user.click(await screen.findByRole("button", { name: "Отменить запись" }));
    expect(chips()).toBeNull();
    expect(track.stop).toHaveBeenCalled();

    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi
          .fn()
          .mockRejectedValue(Object.assign(new Error("denied"), { name: "NotAllowedError" })),
      },
    });
    await user.click(screen.getByRole("button", { name: "Записать голосовое сообщение" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Нет доступа к микрофону/);
  });

  it("браузер без MediaRecorder: кнопки записи нет", async () => {
    vi.stubGlobal("MediaRecorder", undefined);
    await openNewChat();
    expect(screen.queryByRole("button", { name: "Записать голосовое сообщение" })).toBeNull();
    expect(userEvent).toBeDefined();
    void ask;
  });
});

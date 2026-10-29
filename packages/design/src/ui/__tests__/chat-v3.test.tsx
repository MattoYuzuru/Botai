import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NBSP } from "../../lib/format";
import { AttachmentChip } from "../attachment-chip/attachment-chip";
import { AttachmentViewer } from "../attachment-viewer/attachment-viewer";
import type { ComposerAttachment, ViewerAttachment } from "../attachments";
import { ChatComposer } from "../chat-composer/chat-composer";
import { ChatDropZone } from "../chat-drop-zone/chat-drop-zone";
import { ChatMessage } from "../chat-message/chat-message";
import { CitationGroups } from "../citation-groups/citation-groups";
import { PlanCard, type PlanStep } from "../plan-card/plan-card";
import { QuickReplies } from "../quick-replies/quick-replies";
import { TaskCard } from "../task-card/task-card";
import { UnreadDot } from "../unread-dot/unread-dot";

const NOW = Date.UTC(2026, 9, 27, 11, 5);
const plain = (text: string | null) => (text ?? "").replaceAll(NBSP, " ");
const file = (name: string, type = "application/pdf", body = "x") => new File([body], name, { type });

const ready = (
  id: string,
  name: string,
  kind: ComposerAttachment["kind"] = "document",
): ComposerAttachment => ({
  id,
  name,
  mime: "application/pdf",
  size: 2048,
  kind,
  status: "ready",
});

function Harness(
  props: Partial<React.ComponentProps<typeof ChatComposer>> & {
    initial?: string;
    onSendSpy?: (t: string) => void;
  },
) {
  const [value, setValue] = useState(props.initial ?? "");
  const { initial: _initial, onSendSpy, ...rest } = props;
  return <ChatComposer value={value} onValueChange={setValue} onSend={onSendSpy ?? (() => {})} {...rest} />;
}

const input = (container: HTMLElement, name: "file" | "photo" | "camera") =>
  container.querySelector<HTMLInputElement>(`[data-composer-${name}]`) as HTMLInputElement;

describe("ChatComposer: вложения", () => {
  it("выбор файла, фото и снимок с камеры: источник и сами файлы доходят до onAddFiles", async () => {
    const onAddFiles = vi.fn();
    const { container } = render(<Harness onAddFiles={onAddFiles} />);
    const a = file("отчёт.pdf");
    await userEvent.upload(input(container, "file"), a);
    expect(onAddFiles).toHaveBeenLastCalledWith([a], "picker");
    const photo = file("фото.png", "image/png");
    await userEvent.upload(input(container, "photo"), photo);
    expect(onAddFiles).toHaveBeenLastCalledWith([photo], "picker");
    const shot = file("снимок.jpg", "image/jpeg");
    await userEvent.upload(input(container, "camera"), shot);
    expect(onAddFiles).toHaveBeenLastCalledWith([shot], "camera");
    // Камера просит снимок, а не любой файл.
    expect(input(container, "camera")).toHaveAttribute("capture", "environment");
    expect(input(container, "camera")).toHaveAttribute("accept", "image/*");
    // Тот же файл можно выбрать ещё раз.
    expect(input(container, "file").value).toBe("");
  });

  it("«+» без касания открывает выбор файла сразу", async () => {
    const onAddFiles = vi.fn();
    const { container } = render(<Harness onAddFiles={onAddFiles} />);
    const click = vi.spyOn(input(container, "file"), "click");
    await userEvent.click(screen.getByRole("button", { name: "Прикрепить файл" }));
    expect(click).toHaveBeenCalledTimes(1);
  });

  it("на касательном экране «+» открывает меню «Файл», «Фото», «Снять фото»", async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      ...original(query),
      matches: query.includes("coarse"),
    })) as typeof window.matchMedia;
    try {
      const onAddFiles = vi.fn();
      const { container } = render(<Harness onAddFiles={onAddFiles} />);
      const camera = vi.spyOn(input(container, "camera"), "click");
      await userEvent.click(screen.getByRole("button", { name: "Прикрепить файл" }));
      expect(await screen.findByRole("menuitem", { name: "Файл" })).toBeInTheDocument();
      expect(screen.getByRole("menuitem", { name: "Фото" })).toBeInTheDocument();
      await userEvent.click(screen.getByRole("menuitem", { name: "Снять фото" }));
      expect(camera).toHaveBeenCalledTimes(1);
    } finally {
      window.matchMedia = original;
    }
  });

  it("вставка файла из буфера прикрепляет его, обычный текст вставляется как всегда", () => {
    const onAddFiles = vi.fn();
    render(<Harness onAddFiles={onAddFiles} />);
    const area = screen.getByRole("textbox", { name: "Сообщение агенту" });
    const image = file("скрин.png", "image/png");
    const withFile = fireEvent.paste(area, { clipboardData: { files: [image], types: ["Files"] } });
    expect(withFile).toBe(false);
    expect(onAddFiles).toHaveBeenCalledWith([image], "paste");
    onAddFiles.mockClear();
    const textOnly = fireEvent.paste(area, { clipboardData: { files: [], types: ["text/plain"] } });
    expect(textOnly).toBe(true);
    expect(onAddFiles).not.toHaveBeenCalled();
  });

  it("чипы: имя, размер, «Убрать» вызывает onRemoveAttachment, тело открывает вьюер", async () => {
    const onRemove = vi.fn();
    const onOpen = vi.fn();
    render(
      <Harness
        onAddFiles={() => {}}
        attachments={[ready("a1", "Правила.pdf"), ready("a2", "Конспект.docx")]}
        onRemoveAttachment={onRemove}
        onOpenAttachment={onOpen}
      />,
    );
    const list = screen.getByRole("list", { name: "Вложения" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(plain(list.textContent)).toContain("2 КБ");
    await userEvent.click(screen.getByRole("button", { name: "Открыть Правила.pdf" }));
    expect(onOpen).toHaveBeenCalledWith("a1");
    await userEvent.click(screen.getByRole("button", { name: "Убрать Конспект.docx" }));
    expect(onRemove).toHaveBeenCalledWith("a2");
  });

  it("отправка с пустым текстом разрешена, если есть готовое вложение", async () => {
    const onSend = vi.fn();
    render(<Harness onSendSpy={onSend} attachments={[ready("a1", "Правила.pdf")]} onAddFiles={() => {}} />);
    const send = screen.getByRole("button", { name: "Отправить" });
    expect(send).toBeEnabled();
    await userEvent.click(send);
    expect(onSend).toHaveBeenCalledWith("");
  });

  it("пока вложение грузится, отправить нельзя; процент показан только когда он известен", () => {
    const uploading: ComposerAttachment = {
      id: "u1",
      name: "Большой.pdf",
      mime: "application/pdf",
      size: 5_000_000,
      kind: "document",
      status: "uploading",
      progress: 0.4,
    };
    const { rerender } = render(
      <Harness
        initial="Смотри"
        attachments={[ready("a1", "Правила.pdf"), uploading]}
        onAddFiles={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
    const bar = screen.getByRole("progressbar", { name: "Загрузка Большой.pdf" });
    expect(bar).toHaveAttribute("aria-valuenow", "40");
    expect(plain(screen.getByText(/Загрузка \d+ %/).textContent)).toBe("Загрузка 40 %");
    rerender(
      <Harness
        initial="Смотри"
        attachments={[{ ...uploading, progress: undefined }]}
        onAddFiles={() => {}}
      />,
    );
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByText("Загрузка…")).toBeInTheDocument();
  });

  it("не удавшееся вложение: ошибка словами, «Убрать» на месте, отправка текста не заблокирована", async () => {
    const onRemove = vi.fn();
    const onSend = vi.fn();
    render(
      <Harness
        initial="Вопрос"
        onSendSpy={onSend}
        onAddFiles={() => {}}
        onRemoveAttachment={onRemove}
        attachments={[{ ...ready("f1", "Тяжёлый.pdf"), status: "failed", error: "Файл больше 20 МБ" }]}
      />,
    );
    expect(screen.getByText("Файл больше 20 МБ")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Убрать Тяжёлый.pdf" }));
    expect(onRemove).toHaveBeenCalledWith("f1");
  });

  it("предел вложений: «+» недоступен, сказано почему, лишние файлы не уходят", async () => {
    const onAddFiles = vi.fn();
    const { container, rerender } = render(
      <Harness onAddFiles={onAddFiles} maxAttachments={2} attachments={[ready("a", "A.pdf")]} />,
    );
    const a = file("1.pdf");
    const b = file("2.pdf");
    await userEvent.upload(input(container, "file"), [a, b]);
    // Место ровно под один файл.
    expect(onAddFiles).toHaveBeenCalledWith([a], "picker");
    rerender(
      <Harness
        onAddFiles={onAddFiles}
        maxAttachments={2}
        attachments={[ready("a", "A.pdf"), ready("b", "B.pdf")]}
      />,
    );
    expect(screen.getByRole("button", { name: "Прикрепить файл" })).toBeDisabled();
    expect(plain(screen.getByText(/не больше/).textContent)).toBe(
      "К сообщению можно прикрепить не больше 2 файлов.",
    );
  });

  it("без onAddFiles композер как раньше: ни «+», ни микрофона", () => {
    render(<Harness />);
    expect(screen.queryByRole("button", { name: "Прикрепить файл" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Записать голосовое сообщение" })).toBeNull();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  });

  it("notice стоит над полем", () => {
    render(<Harness notice="Ждёт вашего ответа" />);
    expect(screen.getByText("Ждёт вашего ответа")).toBeInTheDocument();
  });
});

describe("AttachmentChip", () => {
  it("загружающийся чип не открывается", () => {
    const onOpen = vi.fn();
    render(
      <AttachmentChip
        attachment={{ ...ready("u", "Файл.pdf"), status: "uploading" }}
        onOpen={onOpen}
        onRemove={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Открыть Файл.pdf" })).toBeNull();
    expect(screen.getByRole("button", { name: "Убрать Файл.pdf" })).toBeEnabled();
  });
});

describe("ChatComposer: микрофон", () => {
  class FakeRecorder {
    static instances: FakeRecorder[] = [];
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
      FakeRecorder.instances.push(this);
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
  const track = { stop: vi.fn() };
  let getUserMedia: ReturnType<typeof vi.fn>;
  const original = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");

  beforeEach(() => {
    FakeRecorder.instances = [];
    track.stop.mockClear();
    getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [track] });
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    if (original) Object.defineProperty(navigator, "mediaDevices", original);
    else Reflect.deleteProperty(navigator, "mediaDevices");
  });

  const mic = () => screen.getByRole("button", { name: "Записать голосовое сообщение" });

  it("запись: индикатор со временем, остановка прикрепляет файл как голос", async () => {
    const onAddFiles = vi.fn();
    render(<Harness onAddFiles={onAddFiles} />);
    await userEvent.click(mic());
    expect(await screen.findByText(/Идёт запись · 0:00/)).toBeInTheDocument();
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(FakeRecorder.instances[0]?.mimeType).toBe("audio/webm;codecs=opus");
    await userEvent.click(screen.getByRole("button", { name: "Остановить и прикрепить" }));
    await waitFor(() => expect(onAddFiles).toHaveBeenCalledTimes(1));
    const [files, source] = onAddFiles.mock.calls[0] as [File[], string];
    expect(source).toBe("voice");
    expect(files).toHaveLength(1);
    expect(files[0]?.name).toMatch(/^Голосовое сообщение \d\d\.\d\d \d\d-\d\d\.webm$/);
    expect(files[0]?.type).toBe("audio/webm");
    expect(track.stop).toHaveBeenCalled();
    expect(screen.queryByText(/Идёт запись/)).toBeNull();
    // Поле снова на месте.
    expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toBeVisible();
  });

  it("отмена записи бросает звук: файла нет, микрофон отпущен", async () => {
    const onAddFiles = vi.fn();
    render(<Harness onAddFiles={onAddFiles} />);
    await userEvent.click(mic());
    await screen.findByText(/Идёт запись/);
    await userEvent.click(screen.getByRole("button", { name: "Отменить запись" }));
    expect(onAddFiles).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
    expect(screen.queryByText(/Идёт запись/)).toBeNull();
  });

  it("время записи идёт и показано цифрами одной ширины", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    try {
      render(<Harness onAddFiles={() => {}} />);
      fireEvent.click(mic());
      await act(async () => {
        await Promise.resolve();
      });
      await act(async () => {
        vi.advanceTimersByTime(7_000);
      });
      const label = screen.getByText(/Идёт запись/);
      expect(plain(label.textContent)).toBe("Идёт запись · 0:07");
      expect(label).toHaveAttribute("data-numeric");
    } finally {
      vi.useRealTimers();
    }
  });

  it("отказ в микрофоне: сообщение в role=alert, запись не началась", async () => {
    getUserMedia.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    render(<Harness onAddFiles={() => {}} />);
    await userEvent.click(mic());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Нет доступа к микрофону. Разрешите его в настройках браузера.");
    expect(screen.queryByText(/Идёт запись/)).toBeNull();
    await userEvent.click(within(alert).getByRole("button", { name: "Закрыть" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("микрофон есть, пока нет ни текста, ни вложений; потом «Отправить»", () => {
    const { rerender } = render(<Harness onAddFiles={() => {}} />);
    expect(mic()).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отправить" })).toBeNull();
    rerender(<Harness onAddFiles={() => {}} attachments={[ready("a", "A.pdf")]} />);
    expect(screen.queryByRole("button", { name: "Записать голосовое сообщение" })).toBeNull();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeInTheDocument();
  });

  it("voice={false} и браузер без MediaRecorder скрывают микрофон", () => {
    const { unmount } = render(<Harness onAddFiles={() => {}} voice={false} />);
    expect(screen.queryByRole("button", { name: "Записать голосовое сообщение" })).toBeNull();
    unmount();
    vi.stubGlobal("MediaRecorder", undefined);
    render(<Harness onAddFiles={() => {}} />);
    expect(screen.queryByRole("button", { name: "Записать голосовое сообщение" })).toBeNull();
  });
});

describe("ChatDropZone", () => {
  const zone = () => document.querySelector("[data-drop-zone]") as HTMLElement;

  it("файлы над областью: оверлей; drop отдаёт файлы с источником drop", () => {
    const onFiles = vi.fn();
    render(
      <ChatDropZone onFiles={onFiles}>
        <p>Чат</p>
      </ChatDropZone>,
    );
    const dropped = file("лекция.pdf");
    fireEvent.dragEnter(zone(), { dataTransfer: { types: ["Files"] } });
    expect(screen.getByText("Отпустите файлы, чтобы прикрепить")).toBeInTheDocument();
    fireEvent.drop(zone(), { dataTransfer: { types: ["Files"], files: [dropped] } });
    expect(onFiles).toHaveBeenCalledWith([dropped], "drop");
    expect(screen.queryByText("Отпустите файлы, чтобы прикрепить")).toBeNull();
  });

  it("вложенные dragenter/dragleave не мигают оверлеем", () => {
    render(
      <ChatDropZone onFiles={() => {}}>
        <p>Чат</p>
      </ChatDropZone>,
    );
    const data = { dataTransfer: { types: ["Files"] } };
    fireEvent.dragEnter(zone(), data);
    fireEvent.dragEnter(screen.getByText("Чат"), data);
    fireEvent.dragLeave(zone(), data);
    expect(screen.getByText("Отпустите файлы, чтобы прикрепить")).toBeInTheDocument();
    fireEvent.dragLeave(screen.getByText("Чат"), data);
    expect(screen.queryByText("Отпустите файлы, чтобы прикрепить")).toBeNull();
  });

  it("перетаскиваемый текст и ссылки игнорируются", () => {
    const onFiles = vi.fn();
    render(
      <ChatDropZone onFiles={onFiles}>
        <p>Чат</p>
      </ChatDropZone>,
    );
    fireEvent.dragEnter(zone(), { dataTransfer: { types: ["text/plain"] } });
    expect(screen.queryByText("Отпустите файлы, чтобы прикрепить")).toBeNull();
    fireEvent.drop(zone(), { dataTransfer: { types: ["text/plain"], files: [] } });
    expect(onFiles).not.toHaveBeenCalled();
  });

  it("disabled: оверлея и вызова нет", () => {
    const onFiles = vi.fn();
    render(
      <ChatDropZone onFiles={onFiles} disabled>
        <p>Чат</p>
      </ChatDropZone>,
    );
    fireEvent.dragEnter(zone(), { dataTransfer: { types: ["Files"] } });
    expect(screen.queryByText("Отпустите файлы, чтобы прикрепить")).toBeNull();
    fireEvent.drop(zone(), { dataTransfer: { types: ["Files"], files: [file("a.pdf")] } });
    expect(onFiles).not.toHaveBeenCalled();
  });
});

describe("AttachmentViewer", () => {
  const items: ViewerAttachment[] = [
    { id: "i", name: "Схема.png", mime: "image/png", size: 1000, kind: "image", url: "/files/i" },
    {
      id: "p",
      name: "Правила.pdf",
      mime: "application/pdf",
      size: 2_000_000,
      kind: "document",
      url: "/files/p",
    },
    { id: "a", name: "Голосовое.webm", mime: "audio/webm", size: 30_000, kind: "audio", url: "/files/a" },
    {
      id: "o",
      name: "Таблица.xlsx",
      mime: "application/vnd.ms-excel",
      size: 4096,
      kind: "other",
      url: "/files/o",
    },
  ];

  it("закрыт, пока openId пуст", () => {
    render(<AttachmentViewer items={items} openId={null} onOpenChange={() => {}} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("изображение — лайтбокс с alt, скачивание и новая вкладка", () => {
    render(<AttachmentViewer items={items} openId="i" onOpenChange={() => {}} />);
    const dialog = screen.getByRole("dialog", { name: "Схема.png" });
    expect(within(dialog).getByRole("img", { name: "Схема.png" })).toHaveAttribute("src", "/files/i");
    expect(within(dialog).getByRole("link", { name: "Скачать" })).toHaveAttribute("download", "Схема.png");
    const tab = within(dialog).getByRole("link", { name: "Открыть в новой вкладке" });
    expect(tab).toHaveAttribute("target", "_blank");
    expect(tab).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("PDF — встроенный просмотр и запасная ссылка", () => {
    render(<AttachmentViewer items={items} openId="p" onOpenChange={() => {}} />);
    expect(screen.getByTitle("Правила.pdf")).toHaveAttribute("src", "/files/p");
    expect(screen.getByRole("link", { name: "Открыть PDF" })).toHaveAttribute("href", "/files/p");
  });

  it("аудио — плеер", () => {
    const { baseElement } = render(<AttachmentViewer items={items} openId="a" onOpenChange={() => {}} />);
    const audio = baseElement.querySelector("audio");
    expect(audio).toHaveAttribute("src", "/files/a");
    expect(audio).toHaveAttribute("controls");
  });

  it("остальное — «Предпросмотр недоступен» и скачивание", () => {
    render(<AttachmentViewer items={items} openId="o" onOpenChange={() => {}} />);
    expect(screen.getByText("Предпросмотр недоступен")).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: /Скачать/ });
    expect(links.length).toBeGreaterThanOrEqual(1);
    expect(links.every((link) => link.getAttribute("href") === "/files/o")).toBe(true);
  });

  it("листание кнопками и стрелками, Esc закрывает", async () => {
    const onOpenChange = vi.fn();
    render(<AttachmentViewer items={items} openId="p" onOpenChange={onOpenChange} />);
    expect(plain(screen.getByText(/2 из 4/).textContent)).toContain("2 из 4");
    await userEvent.click(screen.getByRole("button", { name: "Следующее вложение" }));
    expect(onOpenChange).toHaveBeenLastCalledWith("a");
    await userEvent.click(screen.getByRole("button", { name: "Предыдущее вложение" }));
    expect(onOpenChange).toHaveBeenLastCalledWith("i");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "ArrowRight" });
    expect(onOpenChange).toHaveBeenLastCalledWith("a");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "ArrowLeft" });
    expect(onOpenChange).toHaveBeenLastCalledWith("i");
    await userEvent.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenLastCalledWith(null);
  });

  it("на краях списка соседней кнопки нет в действии, одно вложение — без листания", () => {
    const { rerender } = render(<AttachmentViewer items={items} openId="i" onOpenChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Предыдущее вложение" })).toBeDisabled();
    rerender(<AttachmentViewer items={items.slice(0, 1)} openId="i" onOpenChange={() => {}} />);
    expect(screen.queryByRole("button", { name: "Следующее вложение" })).toBeNull();
  });
});

describe("ChatMessage v3", () => {
  const attachments: ViewerAttachment[] = [
    { id: "i1", name: "Схема.png", mime: "image/png", size: 1000, kind: "image", url: "/files/i1" },
    { id: "i2", name: "Доска.jpg", mime: "image/jpeg", size: 1000, kind: "image", url: "/files/i2" },
    {
      id: "p",
      name: "Правила.pdf",
      mime: "application/pdf",
      size: 2_097_152,
      kind: "document",
      url: "/files/p",
    },
  ];

  it("ответ агента — плоский текст без карточки и рамки", () => {
    const { container } = render(<ChatMessage role="agent" markdown="Пары сегодня в 9:30" />);
    expect(container.querySelector("[data-card]")).toBeNull();
    expect(container.querySelector(".border")).toBeNull();
    expect(screen.getByText("Пары сегодня в 9:30")).toBeInTheDocument();
  });

  it("форма вопроса внутри ответа остаётся карточкой", () => {
    const { container } = render(
      <ChatMessage role="agent" markdown="Это ваши предметы?">
        <button type="button">Да</button>
      </ChatMessage>,
    );
    expect(container.querySelector("[data-card]")).not.toBeNull();
  });

  it("сообщение человека — плашка справа, вложения над текстом; нажатие открывает вьюер", async () => {
    const onOpen = vi.fn();
    const { container } = render(
      <ChatMessage
        role="user"
        markdown="Посмотри"
        attachments={attachments}
        onOpenAttachment={onOpen}
        at={NOW}
      />,
    );
    const article = screen.getByRole("article");
    expect(article).toHaveAttribute("data-role", "user");
    const bubble = container.querySelector(".bg-accent-subtle");
    const files = container.querySelector("[data-message-attachments]");
    expect(bubble).not.toBeNull();
    expect(files?.compareDocumentPosition(bubble as Element)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    const grid = screen.getByRole("list", { name: "Изображения" });
    expect(within(grid).getAllByRole("listitem")).toHaveLength(2);
    expect(within(grid).getByRole("img", { name: "Схема.png" })).toHaveAttribute("src", "/files/i1");
    await userEvent.click(screen.getByRole("button", { name: "Открыть Доска.jpg" }));
    expect(onOpen).toHaveBeenLastCalledWith("i2");
    const rows = screen.getByRole("list", { name: "Файлы" });
    expect(plain(rows.textContent)).toContain("Правила.pdf");
    expect(plain(rows.textContent)).toContain("Документ · 2 МБ");
    await userEvent.click(within(rows).getByRole("button", { name: "Открыть Правила.pdf" }));
    expect(onOpen).toHaveBeenLastCalledWith("p");
  });

  it("только вложение без текста: плашки текста нет", () => {
    const { container } = render(<ChatMessage role="user" markdown="" attachments={attachments.slice(2)} />);
    expect(container.querySelector(".bg-accent-subtle")).toBeNull();
    expect(screen.getByRole("button", { name: "Открыть Правила.pdf" })).toBeInTheDocument();
  });

  it("внутренние ссылки текста идут через resolveLink", () => {
    const resolveLink = vi.fn((url: string) =>
      url === "botai://threads/T1" ? { href: "/chat?thread=T1", label: "Подключение ЦУ" } : null,
    );
    render(<ChatMessage role="agent" markdown="Продолжим в botai://threads/T1" resolveLink={resolveLink} />);
    const link = screen.getByRole("link", { name: "Подключение ЦУ" });
    expect(link).toHaveAttribute("href", "/chat?thread=T1");
    expect(link).not.toHaveAttribute("target");
  });

  it("источники — группы под ответом", () => {
    render(
      <ChatMessage
        role="agent"
        markdown="Ответ"
        nowMs={NOW}
        citationGroups={[
          {
            id: "g",
            label: "Личный кабинет ВШЭ",
            fetchedAt: NOW - 60_000,
            items: [{ id: "1", title: "Расписание", url: "https://lk.fakeuni.test/timetable" }],
          },
        ]}
      />,
    );
    expect(screen.getByRole("list", { name: "Источники" })).toBeInTheDocument();
  });
});

describe("CitationGroups", () => {
  const url = (n: number) => `https://lk.fakeuni.test/p${n}`;
  const group = {
    id: "lk",
    label: "Личный кабинет ВШЭ",
    spaceTitle: "ВШЭ",
    fetchedAt: NOW - 12 * 60_000,
    items: [
      { id: "1", title: "Расписание", url: url(1) },
      { id: "2", title: "Аудитории", url: url(2) },
      { id: "3", title: "Преподаватели", url: null },
    ],
  };

  it("три записи одного источника — одна строка с названием источника, не хостом", () => {
    render(<CitationGroups groups={[group]} nowMs={NOW} />);
    const list = screen.getByRole("list", { name: "Источники" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByText("Личный кабинет ВШЭ")).toBeInTheDocument();
    expect(plain(list.textContent)).toContain("3 страницы");
    expect(plain(list.textContent)).toContain("Получено 12 минут назад");
    expect(list.textContent).not.toContain("lk.fakeuni.test");
    expect(within(list).queryAllByRole("link")).toHaveLength(0);
  });

  it("строка раскрывается списком; недоступная запись без ссылки", async () => {
    render(<CitationGroups groups={[group]} nowMs={NOW} />);
    const toggle = screen.getByRole("button", { name: /Личный кабинет ВШЭ/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const pages = screen.getByRole("list", { name: "Страницы: Личный кабинет ВШЭ" });
    const links = within(pages).getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute("href", url(1));
    expect(links[0]).toHaveAttribute("target", "_blank");
    expect(links[0]).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(within(pages).getByText("Источник недоступен")).toBeInTheDocument();
    await userEvent.click(toggle);
    expect(screen.queryByRole("list", { name: /Страницы/ })).toBeNull();
  });

  it("один источник с одной ссылкой — вся строка ссылка; устаревший помечен", () => {
    render(
      <CitationGroups
        nowMs={NOW}
        groups={[{ ...group, stale: true, items: [{ id: "1", title: "Расписание", url: url(1) }] }]}
      />,
    );
    const link = screen.getByRole("link", { name: /Личный кабинет ВШЭ/ });
    expect(link).toHaveAttribute("href", url(1));
    expect(within(link).getByText("Устарело")).toBeInTheDocument();
  });

  it("внутренняя ссылка открывается в приложении, Ctrl оставляет браузеру", async () => {
    const onOpen = vi.fn();
    render(
      <CitationGroups
        nowMs={NOW}
        groups={[
          {
            ...group,
            items: [
              {
                id: "d",
                title: "Правила",
                url: "botai://documents/d",
                internal: {
                  href: "/study/documents",
                  label: "Открыть документ в «Учёбе»",
                  icon: "document",
                  onOpen,
                },
              },
            ],
          },
        ]}
      />,
    );
    const link = screen.getByRole("link", { name: /Личный кабинет ВШЭ/ });
    expect(link).toHaveAttribute("href", "/study/documents");
    expect(link).not.toHaveAttribute("target");
    await userEvent.click(link);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("недоступный единственный адрес — подпись без ссылки; пустой список — ничего", () => {
    const { container, rerender } = render(
      <CitationGroups
        nowMs={NOW}
        groups={[{ ...group, items: [{ id: "1", title: "Старый портал", url: "botai://sources/3" }] }]}
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Источник недоступен")).toBeInTheDocument();
    rerender(<CitationGroups nowMs={NOW} groups={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("PlanCard", () => {
  it("состояния шагов словами, счёт по настоящим состояниям", () => {
    const { container } = render(
      <PlanCard
        title="Подготовить сводку по сессии"
        steps={[
          { id: "1", title: "Собрать расписание экзаменов", state: "done" },
          { id: "2", title: "Найти сроки курсовых", state: "done" },
          { id: "3", title: "Сверить с документами", state: "active" },
          { id: "4", title: "Составить таблицу", state: "pending" },
          { id: "5", title: "Проверить онлайн-ресурсы", state: "skipped" },
        ]}
      />,
    );
    expect(plain(screen.getByText(/^План/).textContent)).toBe("План · выполнено 2 из 5");
    expect(screen.getByText("Подготовить сводку по сессии")).toBeInTheDocument();
    const steps = within(screen.getByRole("list", { name: "Шаги плана" })).getAllByRole("listitem");
    expect(steps.map((step) => step.getAttribute("data-state"))).toEqual([
      "done",
      "done",
      "active",
      "pending",
      "skipped",
    ]);
    expect(steps[0]).toHaveTextContent("выполнено");
    expect(steps[2]).toHaveTextContent("в работе");
    expect(steps[3]).toHaveTextContent("ждёт");
    expect(steps[4]).toHaveTextContent("пропущено");
    expect(container.querySelector("[data-plan]")).not.toBeNull();
  });

  it("новый блок с тем же планом меняет состояния на месте", () => {
    const steps: PlanStep[] = [
      { id: "1", title: "Шаг", state: "active" },
      { id: "2", title: "Ещё шаг", state: "pending" },
    ];
    const { rerender } = render(<PlanCard title="План" steps={steps} />);
    expect(screen.getByText(/выполнено 0 из 2/)).toBeInTheDocument();
    rerender(
      <PlanCard
        title="План"
        steps={[
          { id: "1", title: "Шаг", state: "done" },
          { id: "2", title: "Ещё шаг", state: "active" },
        ]}
      />,
    );
    expect(screen.getByText(/выполнено 1 из 2/)).toBeInTheDocument();
  });
});

describe("QuickReplies", () => {
  it("кнопки отправляют свой текст, лишнее сверх четырёх не показывается", async () => {
    const onPick = vi.fn();
    render(<QuickReplies options={["Да", "Нет", "Позже", "Покажи расписание", "Лишнее"]} onPick={onPick} />);
    const group = screen.getByRole("group", { name: "Быстрые ответы" });
    expect(within(group).getAllByRole("button")).toHaveLength(4);
    expect(screen.queryByText("Лишнее")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Покажи расписание" }));
    expect(onPick).toHaveBeenCalledWith("Покажи расписание");
  });

  it("disabled блокирует кнопки; пустой список — ничего", () => {
    const { container, rerender } = render(<QuickReplies options={["Да"]} onPick={() => {}} disabled />);
    expect(screen.getByRole("button", { name: "Да" })).toBeDisabled();
    rerender(<QuickReplies options={[]} onPick={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("TaskCard", () => {
  it("задача в этом чате: без подписи «в другом чате»; в слоте — её собственное действие", () => {
    const { container } = render(
      <TaskCard
        title="Подключение ЦУ"
        kind="connect"
        state="running"
        action={<button type="button">Отменить подключение</button>}
      />,
    );
    expect(screen.getByText("Подключение")).toBeInTheDocument();
    expect(screen.getByText("Подключение ЦУ")).toBeInTheDocument();
    expect(screen.getByText("Работает")).toBeInTheDocument();
    expect(screen.queryByText("Идёт в другом чате")).toBeNull();
    expect(screen.getByRole("button", { name: "Отменить подключение" })).toBeInTheDocument();
    expect(container.querySelector("[data-task-card]")).toHaveAttribute("data-state", "running");
  });

  it("задача в другом чате: подпись и действие из слота", () => {
    render(
      <TaskCard
        title="Smart LMS"
        kind="repair"
        state="waiting_user"
        here={false}
        action={<a href="/chat?thread=T2">Открыть чат</a>}
      />,
    );
    expect(screen.getByText("Починка")).toBeInTheDocument();
    expect(screen.getByText("Ждёт вас")).toBeInTheDocument();
    expect(screen.getByText("Идёт в другом чате")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть чат" })).toHaveAttribute("href", "/chat?thread=T2");
  });

  it("все состояния названы словами", () => {
    const labels = {
      waiting_user: "Ждёт вас",
      running: "Работает",
      done: "Готово",
      failed: "Не удалось",
      queued: "В очереди",
      cancelled: "Отменено",
    } as const;
    for (const [state, label] of Object.entries(labels)) {
      const { unmount } = render(
        <TaskCard title="Задача" kind="sync" state={state as keyof typeof labels} />,
      );
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });
});

describe("UnreadDot", () => {
  it("подпись для скринридера и размер 8 px", () => {
    render(<UnreadDot />);
    const dot = screen.getByRole("img", { name: "Есть непрочитанное" });
    expect(dot).toHaveClass("size-2", "bg-accent", "rounded-full");
  });
});

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ChatComposerProps } from "../chat-composer/chat-composer";
import { ChatComposer } from "../chat-composer/chat-composer";

const PHRASE = "Помоги составить план подготовки";
function Harness(props: Partial<ChatComposerProps>) {
  const [value, setValue] = useState("");
  return (
    <ChatComposer value={value} onValueChange={setValue} onSend={() => {}} suggestion={PHRASE} {...props} />
  );
}
const field = () => screen.getByRole("textbox", { name: "Сообщение агенту" });

describe("ChatComposer: продолжение разговора", () => {
  it("призрачная фраза не является значением; → вставляет редактируемый текст, не отправляет", async () => {
    const send = vi.fn();
    render(<Harness onSend={send} enterToSend />);
    expect(field()).toHaveValue("");
    expect(screen.getByText(PHRASE)).toHaveAttribute("aria-hidden", "true");
    expect(field()).toHaveAccessibleDescription(expect.stringContaining(PHRASE));
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(send).not.toHaveBeenCalled();
    expect(field()).toHaveValue("");
    fireEvent.keyDown(field(), { key: "ArrowRight" });
    expect(field()).toHaveValue(PHRASE);
    expect(field()).toHaveFocus();
    expect(send).not.toHaveBeenCalled();
    expect(document.querySelector("[data-composer-suggestion]")).toBeNull();
    await userEvent.setup().type(field(), " завтра");
    expect(field()).toHaveValue(`${PHRASE} завтра`);
  });

  it("ввод человека, модификаторы и IME не принимают подсказку", () => {
    render(<Harness />);
    for (const modifier of ["shiftKey", "metaKey", "ctrlKey", "altKey", "isComposing"]) {
      fireEvent.keyDown(field(), { key: "ArrowRight", [modifier]: true });
      expect(field()).toHaveValue("");
    }
    fireEvent.compositionStart(field());
    expect(document.querySelector("[data-composer-suggestion]")).toBeNull();
    fireEvent.keyDown(field(), { key: "ArrowRight" });
    expect(field()).toHaveValue("");
    fireEvent.compositionEnd(field());
    fireEvent.change(field(), { target: { value: "Мой вопрос" } });
    fireEvent.keyDown(field(), { key: "ArrowRight" });
    expect(field()).toHaveValue("Мой вопрос");
  });

  it("занятое поле и любое вложение скрывают подсказку", () => {
    const app = render(<Harness disabled />);
    expect(document.querySelector("[data-composer-suggestion]")).toBeNull();
    app.rerender(
      <Harness
        attachments={[
          { id: "A", name: "a.pdf", mime: "application/pdf", size: 1, kind: "document", status: "uploading" },
        ]}
      />,
    );
    expect(document.querySelector("[data-composer-suggestion]")).toBeNull();
    fireEvent.keyDown(field(), { key: "ArrowRight" });
    expect(field()).toHaveValue("");
  });

  it("на телефоне фраза вставляется компактной кнопкой и получает фокус", async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      ...original(query),
      matches: query.includes("coarse"),
    })) as typeof window.matchMedia;
    try {
      const send = vi.fn();
      render(<Harness onSend={send} />);
      await userEvent.setup().click(screen.getByRole("button", { name: "Вставить подсказку" }));
      expect(field()).toHaveValue(PHRASE);
      expect(field()).toHaveFocus();
      expect(send).not.toHaveBeenCalled();
    } finally {
      window.matchMedia = original;
    }
  });
});

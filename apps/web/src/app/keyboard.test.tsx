import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useComposerKeyboard } from "./keyboard";

function Probe() {
  const open = useComposerKeyboard();
  return (
    <>
      <p data-testid="state">{open ? "open" : "closed"}</p>
      <form data-composer="">
        <textarea aria-label="Сообщение" />
        <button type="button">Прикрепить</button>
      </form>
      <input aria-label="Поиск" />
    </>
  );
}

const original = window.matchMedia;
beforeEach(() => {
  // Телефон: узкий экран и касание.
  window.matchMedia = ((query: string) => ({
    matches: query.includes("pointer: coarse"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
});
afterEach(() => {
  window.matchMedia = original;
});

describe("экранная клавиатура над полем чата", () => {
  it("открывает только фокус в текстовом поле поля ввода; кнопки рядом её не вызывают", () => {
    render(<Probe />);
    const state = () => screen.getByTestId("state").textContent;
    expect(state()).toBe("closed");
    act(() => screen.getByLabelText("Сообщение").focus());
    expect(state()).toBe("open");
    act(() => screen.getByRole("button", { name: "Прикрепить" }).focus());
    expect(state()).toBe("closed");
    act(() => screen.getByLabelText("Сообщение").focus());
    expect(state()).toBe("open");
    // Поле вне поля ввода чата клавиатуру «чата» не открывает.
    act(() => screen.getByLabelText("Поиск").focus());
    expect(state()).toBe("closed");
  });
});

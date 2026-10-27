import { act, isInaccessible, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cycleLines } from "../../mascot/lines";
import { resolveMascot } from "../../mascot/resolve";
import { claimAnimation, holdsAnimation, resetAnimationClaims } from "../../mascot/slots";
import type { MascotContext } from "../../mascot/types";
import { Mascot, MascotAnimationPolicy, type MascotHandle } from "../mascot/mascot";
import { BUBBLE_MAX_CHARS, MascotBubble } from "../mascot-bubble/mascot-bubble";
import { MascotDock } from "../mascot-dock/mascot-dock";

const NOW = Date.UTC(2026, 9, 27, 11, 5);

afterEach(() => resetAnimationClaims());

const svgs = (container: HTMLElement) => [...container.querySelectorAll("svg.varya")];
const isStill = (svg: Element | undefined) => svg?.classList.contains("still") === true;

describe("один анимированный маскот на экран", () => {
  it("очередь: анимацию получает первый, второй стоит, пока первый не уйдёт", () => {
    const a = Symbol("a");
    const b = Symbol("b");
    const releaseA = claimAnimation(a);
    claimAnimation(b);
    expect(holdsAnimation(a)).toBe(true);
    expect(holdsAnimation(b)).toBe(false);
    releaseA();
    expect(holdsAnimation(b)).toBe(true);
  });

  it("две Вари на экране: анимирована одна, вторая — статичная поза; уйдёт первая — вторая оживает", () => {
    const { container, rerender } = render(
      <>
        <Mascot situation="sync" key="first" />
        <Mascot situation="think" key="second" />
      </>,
    );
    const [first, second] = svgs(container);
    expect(isStill(first)).toBe(false);
    expect(isStill(second)).toBe(true);
    // Первая ушла со сцены — анимация переходит ко второй.
    rerender(<Mascot situation="think" key="second" />);
    expect(isStill(svgs(container)[0])).toBe(false);
  });

  it("статичная Варя места не занимает: анимированная рядом остаётся живой", () => {
    const { container } = render(
      <>
        <Mascot situation="done" motion="static" />
        <Mascot situation="sync" />
      </>,
    );
    const [still, live] = svgs(container);
    expect(isStill(still)).toBe(true);
    expect(isStill(live)).toBe(false);
  });

  it("витрина снимает правило: MascotAnimationPolicy exclusive={false}", () => {
    const { container } = render(
      <MascotAnimationPolicy exclusive={false}>
        <Mascot situation="sync" />
        <Mascot situation="think" />
      </MascotAnimationPolicy>,
    );
    expect(svgs(container).every((svg) => !isStill(svg))).toBe(true);
  });
});

describe("MascotBubble", () => {
  it("декор: aria-hidden, до 48 знаков, длиннее — с многоточием", () => {
    const { container, rerender } = render(<MascotBubble text="Ищу в ВШЭ" />);
    const bubble = container.querySelector("[data-mascot-bubble]");
    expect(bubble).toHaveAttribute("aria-hidden", "true");
    expect(bubble).toHaveTextContent("Ищу в ВШЭ");
    rerender(<MascotBubble text={"я".repeat(80)} />);
    const text = container.querySelector("[data-mascot-bubble]")?.textContent ?? "";
    expect([...text]).toHaveLength(BUBBLE_MAX_CHARS);
    expect(text.endsWith("…")).toBe(true);
  });

  it("нажатие закрывает пузырь (обработчик получает событие)", async () => {
    const onClick = vi.fn();
    const { container } = render(<MascotBubble text="Привет" onClick={onClick} />);
    await userEvent.click(container.querySelector("[data-mascot-bubble]") as Element);
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe("MascotDock: пузырь и действия", () => {
  it("пузырь стоит рядом со спрайтом, а не внутри него: масштаб насеста его не уменьшает", () => {
    const { container } = render(
      <MascotDock mode="perch" situation="perch" bubble={<MascotBubble text="Ищу в ВШЭ" />} />,
    );
    const bubble = container.querySelector("[data-dock-bubble]");
    expect(bubble).not.toBeNull();
    const scaled = container.querySelector("svg.varya")?.closest("[class*='origin-bottom-left']");
    expect(scaled?.contains(bubble as Node)).toBe(false);
  });

  it("над сценой пузырь следует за Варей по полу, над насестом — у правого края", () => {
    const bubble = <MascotBubble text="Привет" />;
    const { container, rerender } = render(<MascotDock mode="floor" situation="idle" bubble={bubble} />);
    expect(container.querySelector("[data-dock-bubble]")?.className).toContain("var(--dock-p)");
    rerender(<MascotDock mode="perch" situation="perch" bubble={bubble} />);
    expect(container.querySelector("[data-dock-bubble]")?.className).toContain("right-4");
  });

  it("снаружи можно дать Варе действие: кивок на ответ, взмах крылом", () => {
    vi.useFakeTimers();
    try {
      const ref = createRef<MascotHandle>();
      const { container } = render(<MascotDock ref={ref} mode="perch" situation="perch" />);
      act(() => ref.current?.act("nod"));
      expect(container.querySelector("svg.varya")?.getAttribute("class")).toContain("a-nod");
      act(() => {
        vi.advanceTimersByTime(1_200);
      });
      expect(container.querySelector("svg.varya")?.getAttribute("class")).not.toContain("a-nod");
    } finally {
      vi.useRealTimers();
    }
  });

  it("static: действия не играются", () => {
    const ref = createRef<MascotHandle>();
    const { container } = render(<MascotDock ref={ref} mode="perch" situation="perch" motion="static" />);
    act(() => ref.current?.act("wave"));
    expect(container.querySelector("svg.varya")?.getAttribute("class")).not.toMatch(/\ba-/);
  });
});

describe("следующая реплика по нажатию", () => {
  const fields = { space: "ВШЭ", elapsed: "12 с" };

  it("все подходящие варианты по кругу, а не только самый содержательный", () => {
    const first = "Обновляю ВШЭ, идёт 12 с";
    expect(cycleLines("sync", fields, first, 1)).toBe("Обновляю ВШЭ");
    expect(cycleLines("sync", fields, "Обновляю ВШЭ", 1)).toBe("Обновляю данные");
    expect(cycleLines("sync", fields, "Обновляю данные", 1)).toBe(first);
    // Нет текущей реплики — первая; шаг больше числа вариантов не ломает круг.
    expect(cycleLines("sync", fields, null, 1)).toBe(first);
    expect(cycleLines("sync", fields, first, 7)).toBe("Обновляю ВШЭ");
  });

  it("нет подходящего варианта — null, пузыря нет", () => {
    expect(cycleLines("perch", {}, null, 1)).toBeNull();
    expect(cycleLines("class", {}, null, 1)).toBeNull();
  });

  it("состояние отдаёт запись реестра и поля: из них считается следующая реплика", () => {
    const context: MascotContext = {
      spaceId: "hse",
      spaceTitle: "ВШЭ",
      nowMs: NOW,
      timeZone: "Europe/Moscow",
      screen: "chat",
      availability: "ready",
      activity: "thinking",
      work: null,
      snapshot: null,
      recentEvent: null,
      acknowledgedEventIds: [],
      preferences: { seasonal: true, motion: "system" },
      reducedMotion: false,
      lite: false,
      visible: true,
      keyboardOpen: false,
    };
    const presentation = resolveMascot(context);
    expect(presentation.lineKey).toBe("think");
    expect(presentation.lineFields).toMatchObject({ space: "ВШЭ" });
    expect(cycleLines(presentation.lineKey ?? "idle", presentation.lineFields, presentation.line, 1)).toBe(
      "Ищу в твоих данных",
    );
    // Нет данных вуза — нет и реплики с названием.
    const quiet = resolveMascot({ ...context, spaceTitle: null });
    expect(quiet.line).toBe("Ищу в твоих данных");
  });
});

describe("доступность реплик", () => {
  it("пузырь не входит в дерево доступности: всё, что в нём сказано, уже есть текстом на экране", () => {
    render(<MascotBubble text="Читаю расписание" />);
    expect(isInaccessible(screen.getByText("Читаю расписание"))).toBe(true);
    expect(screen.queryByRole("status")).toBeNull();
  });
});

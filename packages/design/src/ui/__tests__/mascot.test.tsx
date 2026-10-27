import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resolveMascot } from "../../mascot/resolve";
import { stageStep } from "../../mascot/stage-plan";
import type { MascotContext, MascotPresentation } from "../../mascot/types";
import { Mascot } from "../mascot/mascot";
import { HOP_MS, MascotDock } from "../mascot-dock/mascot-dock";
import { MascotStage, REACTION_MS } from "../mascot-stage/mascot-stage";

const NOW = Date.UTC(2026, 9, 27, 11, 5);
const HOUR = 3_600_000;

function context(extra: Partial<MascotContext> = {}): MascotContext {
  return {
    spaceId: "hse",
    spaceTitle: "ВШЭ",
    nowMs: NOW,
    timeZone: "Europe/Moscow",
    screen: "chat",
    availability: "ready",
    activity: "idle",
    work: null,
    snapshot: {
      spaceId: "hse",
      updatedAt: NOW - 300_000,
      stale: false,
      nextClass: null,
      deadlines: [{ id: "u", title: "Домашнее задание 3", dueMs: NOW + 5 * HOUR, done: false }],
    },
    recentEvent: null,
    acknowledgedEventIds: [],
    preferences: { seasonal: true, motion: "system" },
    reducedMotion: false,
    lite: false,
    visible: true,
    keyboardOpen: false,
    ...extra,
  };
}

const svgOf = (container: HTMLElement) => container.querySelector("svg.varya");

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => vi.useRealTimers());

describe("Mascot", () => {
  it("декоративна: aria-hidden, без фокуса, без данных учёбы в разметке", () => {
    const { container } = render(<Mascot situation="deadline" />);
    const host = container.querySelector("[data-mascot]");
    expect(host).toHaveAttribute("aria-hidden", "true");
    expect(host?.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelector("[tabindex], button, a, input")).toBeNull();
  });

  it("монтирует renderer и разбирает его при размонтировании", () => {
    const { container, unmount } = render(<Mascot situation="sync" motion="animated" />);
    expect(svgOf(container)).not.toBeNull();
    expect(svgOf(container)?.classList.contains("l-run")).toBe(true);
    unmount();
    expect(document.querySelector("svg.varya")).toBeNull();
  });

  it("смена ситуации, образа и режима движения идёт в тот же экземпляр", () => {
    const { container, rerender } = render(<Mascot situation="idle" />);
    const first = svgOf(container);
    rerender(<Mascot situation="reauth" skin="leaf" motion="static" />);
    const same = svgOf(container);
    expect(same).toBe(first);
    expect(same?.getAttribute("data-sit")).toBe("reauth");
    expect(same?.classList.contains("skin-leaf")).toBe(true);
    expect(same?.classList.contains("still")).toBe(true);
    rerender(<Mascot situation="reauth" skin={null} motion="animated" />);
    expect(same?.classList.contains("still")).toBe(false);
    expect(same?.classList.contains("skin-leaf")).toBe(false);
  });

  it("смена темы на pj ставит ночной колпак", () => {
    const { container, rerender } = render(<Mascot situation="night" theme="orig" />);
    rerender(<Mascot situation="night" theme="pj" />);
    expect(svgOf(container)?.classList.contains("pj")).toBe(true);
  });

  it("flip зеркалит клюв вправо классом, размер задаёт квадрат", () => {
    const { container } = render(<Mascot situation="sync" flip size={80} />);
    const host = container.querySelector<HTMLElement>("[data-mascot]");
    expect(host?.className).toContain("-scale-x-100");
    expect(host?.style.width).toBe("80px");
    expect(host?.style.height).toBe("80px");
  });
});

describe("MascotStage", () => {
  it("срочная работа: бейдж, реплика и действие — текстом рядом с картинкой", async () => {
    vi.useRealTimers();
    const presentation = resolveMascot(context());
    const onIntent = vi.fn();
    const { container } = render(<MascotStage presentation={presentation} onIntent={onIntent} />);
    expect(screen.getByText(/Осталось\s5\sч/)).toBeInTheDocument();
    expect(screen.getByText(/Домашнее задание 3: осталось/)).toBeInTheDocument();
    expect(svgOf(container)?.getAttribute("data-sit")).toBe("deadline");
    await userEvent.click(screen.getByRole("button", { name: "Открыть срочную" }));
    expect(onIntent).toHaveBeenCalledWith({
      kind: "open-deadline",
      label: "Открыть срочную",
      deadlineId: "u",
    });
  });

  it("без картинки (visible: false) экран остаётся понятным: бейдж, реплика и действие на месте", () => {
    const presentation = resolveMascot(context({ visible: false }));
    expect(presentation.visible).toBe(false);
    const { container } = render(<MascotStage presentation={presentation} />);
    expect(svgOf(container)).toBeNull();
    expect(screen.getByText(/Осталось\s5\sч/)).toBeInTheDocument();
    expect(screen.getByText(/Домашнее задание 3/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открыть срочную" })).toBeInTheDocument();
  });

  it("если нечего сказать и картинка скрыта, не рисует ничего", () => {
    const quiet = resolveMascot(context({ screen: "schedule", snapshot: null }));
    expect(quiet.visible).toBe(false);
    const { container } = render(<MascotStage presentation={{ ...quiet, line: null }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("calm / reducedMotion: статичная поза, тот же смысл", () => {
    const presentation = resolveMascot(context({ reducedMotion: true }));
    const { container } = render(<MascotStage presentation={presentation} />);
    expect(svgOf(container)?.classList.contains("still")).toBe(true);
    expect(screen.getByRole("button", { name: "Открыть срочную" })).toBeInTheDocument();
  });

  it("реакция на событие: одна, потом подтверждение; без картинки не тратится", () => {
    const event = {
      id: "d1",
      spaceId: "hse",
      kind: "deadline-done" as const,
      at: NOW - 1_000,
      title: "Эссе",
    };
    const presentation = resolveMascot(context({ recentEvent: event }));
    expect(presentation.reactionEventId).toBe("d1");
    const onReactionEnd = vi.fn();
    const { rerender, unmount } = render(
      <MascotStage presentation={{ ...presentation, visible: false }} onReactionEnd={onReactionEnd} />,
    );
    act(() => {
      vi.advanceTimersByTime(REACTION_MS * 3);
    });
    expect(onReactionEnd).not.toHaveBeenCalled();
    rerender(<MascotStage presentation={presentation} onReactionEnd={onReactionEnd} />);
    act(() => {
      vi.advanceTimersByTime(REACTION_MS - 1);
    });
    expect(onReactionEnd).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(2);
    });
    expect(onReactionEnd).toHaveBeenCalledTimes(1);
    expect(onReactionEnd).toHaveBeenCalledWith("d1");
    unmount();
  });

  it("не передаёт в renderer ничего, кроме ситуации, темы, образа и размера", () => {
    const presentation: MascotPresentation = {
      ...resolveMascot(context()),
      line: "секретная реплика",
    };
    const { container } = render(<MascotStage presentation={presentation} />);
    const host = container.querySelector("[data-mascot]");
    expect(host?.outerHTML).not.toContain("секретная реплика");
    expect(host?.outerHTML).not.toContain("Домашнее");
  });
});

describe("MascotDock", () => {
  it("насест: слой не перехватывает касания, спрайт — единственное место нажатия", async () => {
    vi.useRealTimers();
    const onTap = vi.fn();
    const { container } = render(<MascotDock mode="perch" situation="perch" onTap={onTap} />);
    const dock = container.querySelector("[data-dock]");
    expect(dock).toHaveAttribute("data-dock", "perch");
    expect(dock?.className).toContain("pointer-events-none");
    const sprite = container.querySelector("svg.varya");
    expect(sprite).not.toBeNull();
    await userEvent.click(sprite as Element);
    expect(onTap).toHaveBeenCalledTimes(1);
  });

  it("пол → насест: тот же экземпляр Вари, прыжок не длиннее 280 мс", () => {
    expect(HOP_MS).toBeLessThanOrEqual(280);
    const { container, rerender } = render(<MascotDock mode="floor" situation="idle" seed={5} />);
    const before = svgOf(container);
    expect(container.querySelector("[data-dock]")).toHaveAttribute("data-dock", "floor");
    rerender(<MascotDock mode="perch" situation="perch" seed={5} />);
    expect(container.querySelector("[data-dock]")).toHaveAttribute("data-dock", "perch");
    expect(svgOf(container)).toBe(before);
    expect(container.querySelectorAll("svg.varya")).toHaveLength(1);
  });

  it("static: переход мгновенный, сцена не играет", () => {
    const { container } = render(<MascotDock mode="floor" situation="idle" motion="static" seed={5} />);
    const moving = container.querySelector<HTMLElement>("[data-dock] > div > div");
    expect(moving?.style.transitionDuration).toBe("0s");
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(svgOf(container)?.classList.contains("still")).toBe(true);
    expect(svgOf(container)?.getAttribute("class") ?? "").not.toMatch(/\ba-/);
  });

  it("сцена: через паузу 6–10 с Варя делает действие, потом возвращается к покою", () => {
    const first = stageStep(5, 0);
    const { container } = render(<MascotDock mode="floor" situation="idle" seed={5} />);
    expect(svgOf(container)?.getAttribute("class") ?? "").not.toMatch(/\ba-/);
    act(() => {
      vi.advanceTimersByTime(first.pauseMs - 100);
    });
    expect(svgOf(container)?.getAttribute("class") ?? "").not.toMatch(/\ba-|\bl-run\b/);
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(svgOf(container)?.getAttribute("class") ?? "").toMatch(/\ba-|\bl-run\b/);
    act(() => {
      vi.advanceTimersByTime(first.durationMs + 100);
    });
    expect(svgOf(container)?.getAttribute("class") ?? "").not.toMatch(/\ba-|\bl-run\b/);
  });

  it("пузырь едет вместе с Варей и сам не прячет её от касаний мимо спрайта", () => {
    const { container } = render(
      <MascotDock mode="perch" situation="perch" bubble={<span>Ищу в ВШЭ</span>} />,
    );
    expect(screen.getByText("Ищу в ВШЭ")).toBeInTheDocument();
    expect(container.querySelector("[data-dock]")?.className).toContain("pointer-events-none");
  });
});

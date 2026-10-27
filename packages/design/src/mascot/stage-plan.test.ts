import { describe, expect, it, vi } from "vitest";
import { STAGE_ACTION_MS, STAGE_ACTIONS, STAGE_PAUSE_MS, stagePlan, stageStep } from "./stage-plan";
import { seededRandom } from "./varya";

describe("сцена пустого чата §4.2", () => {
  it("план детерминирован: одно зерно — один план, другое зерно — другой", () => {
    const random = vi.spyOn(Math, "random").mockImplementation(() => {
      throw new Error("Math.random() вызван");
    });
    const a = stagePlan(1_000_000, 30);
    expect(stagePlan(1_000_000, 30)).toEqual(a);
    expect(stagePlan(1_000_001, 30)).not.toEqual(a);
    expect(random).not.toHaveBeenCalled();
    random.mockRestore();
  });

  it("шаг с любым номером считается сам и совпадает с планом", () => {
    const plan = stagePlan(42, 40);
    for (const index of [0, 7, 19, 39]) expect(stageStep(42, index)).toEqual(plan[index]);
  });

  it("пауза 6–10 с, действие 1.5–2.5 с", () => {
    for (const step of stagePlan(7, 200)) {
      expect(step.pauseMs).toBeGreaterThanOrEqual(STAGE_PAUSE_MS[0]);
      expect(step.pauseMs).toBeLessThanOrEqual(STAGE_PAUSE_MS[1]);
      expect(step.durationMs).toBeGreaterThanOrEqual(STAGE_ACTION_MS[0]);
      expect(step.durationMs).toBeLessThanOrEqual(STAGE_ACTION_MS[1]);
    }
  });

  it("за круг из пяти шагов — все пять действий, и подряд одно и то же не повторяется", () => {
    for (const seed of [1, 2, 99, 1_761_000_000_000]) {
      const plan = stagePlan(seed, 50);
      for (let cycle = 0; cycle < 10; cycle++) {
        const actions = plan.slice(cycle * 5, cycle * 5 + 5).map((step) => step.action);
        expect(new Set(actions)).toEqual(new Set(STAGE_ACTIONS));
      }
      for (let index = 1; index < plan.length; index++) {
        expect(plan[index]?.action, `seed ${seed}, шаг ${index}`).not.toBe(plan[index - 1]?.action);
      }
    }
  });

  it("seededRandom повторяем и лежит в [0, 1)", () => {
    const a = seededRandom(5);
    const b = seededRandom(5);
    for (let i = 0; i < 100; i++) {
      const value = a();
      expect(value).toBe(b());
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThan(1);
    }
  });
});

import { describe, expect, it } from "vitest";
import { type DataStateInput, resolveDataState, toScheduleState } from "./data-state";

const base: DataStateInput = {
  hasData: true,
  isEmpty: false,
  online: true,
  snapshot: false,
  failed: false,
  sources: ["ok"],
  staleItems: false,
};
const state = (patch: Partial<DataStateInput>) => resolveDataState({ ...base, ...patch });

describe("семь состояний данных", () => {
  it("loading — пока данных нет совсем, даже если запрос упал: скелетон остаётся, повторы идут в фоне", () => {
    expect(state({ hasData: false })).toBe("loading");
    expect(state({ hasData: false, failed: true })).toBe("loading");
  });

  it("ok — свежие данные", () => {
    expect(state({})).toBe("ok");
    expect(state({ sources: ["ok", "syncing"] })).toBe("ok");
  });

  it("empty — данные получены и свежи, но показывать нечего", () => {
    expect(state({ isEmpty: true })).toBe("empty");
  });

  it("пустота не прячет условие: при просроченном входе или устаревшем снимке это не «empty»", () => {
    expect(state({ isEmpty: true, sources: ["reauth"] })).toBe("reauth");
    expect(state({ isEmpty: true, sources: ["stale"] })).toBe("stale");
  });

  it("stale — устаревший источник, пометка сервера, снимок из кэша или сбой обновления при живых данных", () => {
    expect(state({ sources: ["stale"] })).toBe("stale");
    expect(state({ staleItems: true })).toBe("stale");
    expect(state({ snapshot: true })).toBe("stale");
    expect(state({ failed: true })).toBe("stale");
  });

  it("offline важнее всего: сеть пропала — показывается последний снимок", () => {
    expect(state({ online: false, sources: ["reauth"] })).toBe("offline");
    expect(state({ online: false, snapshot: true })).toBe("offline");
  });

  it("reauth важнее repairing и stale, repairing важнее stale", () => {
    expect(state({ sources: ["stale", "repairing", "reauth"] })).toBe("reauth");
    expect(state({ sources: ["stale", "repairing"] })).toBe("repairing");
  });

  it("ScheduleDay знает четыре состояния: всё, что не loading/empty/ok, — stale", () => {
    expect(toScheduleState("loading")).toBe("loading");
    expect(toScheduleState("empty")).toBe("empty");
    expect(toScheduleState("ok")).toBe("ok");
    for (const value of ["stale", "offline", "reauth", "repairing"] as const) {
      expect(toScheduleState(value)).toBe("stale");
    }
  });
});

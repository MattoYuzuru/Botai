import { ALL_SPACES_ID } from "@botai/design";
import { describe, expect, it } from "vitest";
import type { Connection, Space } from "@/api/types";
import { resolveActiveSpace } from "./active-space";
import { activeTab, MAIN_TABS } from "./nav";
import { summarizeConnections } from "./space-status";

describe("вкладки §7.1", () => {
  it("ровно пять, в фиксированном порядке", () => {
    expect(MAIN_TABS.map((tab) => tab.id)).toEqual(["today", "schedule", "tasks", "chat", "study"]);
    expect(MAIN_TABS.map((tab) => tab.label)).toEqual(["Сегодня", "Расписание", "Задания", "Чат", "Учёба"]);
  });

  it.each([
    ["/", "today"],
    ["/schedule", "schedule"],
    ["/tasks", "tasks"],
    ["/tasks/01JMKCDB00000000000000000A", "tasks"],
    ["/chat", "chat"],
    ["/study", "study"],
    ["/study/grades", "study"],
    ["/styleguide", null],
    ["/login", null],
    ["/schedule-old", null],
  ])("%s → %s", (pathname, tab) => {
    expect(activeTab(pathname)).toBe(tab);
  });
});

describe("активное пространство", () => {
  const spaces = [{ id: "a" }, { id: "b" }] as Space[];
  it("выбранного вуза нет — сводный экран; нет вузов вовсе — тоже", () => {
    expect(resolveActiveSpace(spaces, "b")).toBe("b");
    expect(resolveActiveSpace(spaces, "gone")).toBe(ALL_SPACES_ID);
    expect(resolveActiveSpace(spaces, ALL_SPACES_ID)).toBe(ALL_SPACES_ID);
    expect(resolveActiveSpace([], "a")).toBe(ALL_SPACES_ID);
    expect(resolveActiveSpace(undefined, "a")).toBe(ALL_SPACES_ID);
  });
});

describe("сводное состояние источников вуза", () => {
  const conn = (status: Connection["status"], lastSyncAt: string | null = null) =>
    ({ status, lastSyncAt }) as Connection;
  const t1 = "2026-10-27T10:00:00Z";
  const t2 = "2026-10-27T11:00:00Z";

  it("нет подключений — none", () => {
    expect(summarizeConnections([], true)).toEqual({ state: "none", updatedAt: null });
    expect(summarizeConnections([conn("disabled")], true).state).toBe("none");
  });

  it("всё активно — ok, время — самое свежее", () => {
    expect(summarizeConnections([conn("active", t1), conn("active", t2)], true)).toEqual({
      state: "ok",
      updatedAt: Date.parse(t2),
    });
  });

  it.each([
    [["active", "needs_reauth"], "reauth"],
    [["needs_reauth", "repairing"], "reauth"],
    [["active", "repairing", "syncing"], "repairing"],
    [["active", "syncing"], "syncing"],
    [["learning"], "syncing"],
    [["waiting_user"], "syncing"],
    [["active", "stale"], "stale"],
    [["error"], "stale"],
  ] as const)("%j → %s", (statuses, expected) => {
    expect(
      summarizeConnections(
        statuses.map((status) => conn(status)),
        true,
      ).state,
    ).toBe(expected);
  });

  it("нет сети — offline важнее всего остального, снимок остаётся", () => {
    expect(summarizeConnections([conn("needs_reauth", t1)], false)).toEqual({
      state: "offline",
      updatedAt: Date.parse(t1),
    });
  });
});

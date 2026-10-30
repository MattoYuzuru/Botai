import { describe, expect, it } from "vitest";
import type { Deadline } from "@/api/types";
import { provenanceFixture } from "@/test/data-fixtures";
import { toDeadlineView } from "./deadlines";
import { GROUP_TITLE, groupDeadlines } from "./groups";

const MSK = "Europe/Moscow";
// Вторник, 27 октября 2026, 17:40 по Москве.
const NOW = Date.UTC(2026, 9, 27, 14, 40);
const HOUR = 3_600_000;

function deadline(
  id: string,
  dueInHours: number | null,
  state: Deadline["state"] = "open",
  effectiveInHours?: number,
): Deadline {
  const at = (hours: number | undefined) =>
    hours === undefined ? null : new Date(NOW + hours * HOUR).toISOString();
  return {
    id,
    title: id,
    dueAt: at(dueInHours ?? undefined),
    effectiveDueAt: at(effectiveInHours),
    state,
    courseTitle: null,
    url: null,
    provenance: provenanceFixture({ fetchedAt: at(-1) as string }),
  };
}

const groups = (rows: Deadline[]) =>
  groupDeadlines(
    rows.map((row) => toDeadlineView(row, NOW)),
    NOW,
    MSK,
  );

describe("группы заданий", () => {
  it("порядок: просрочено, сегодня, на неделе, позже, без срока, сдано; пустые группы скрыты", () => {
    const result = groups([
      deadline("done", -72, "submitted"),
      deadline("none", null),
      deadline("later", 24 * 20),
      deadline("week", 72),
      deadline("today", 5),
      deadline("overdue", -5),
    ]);
    expect(result.map((row) => row.group)).toEqual(["overdue", "today", "week", "later", "none", "done"]);
    expect(groups([deadline("only", 5)]).map((row) => row.group)).toEqual(["today"]);
  });

  it("граница «сегодня» — календарный день вуза, а не 24 часа: срок завтра в 01:00 — уже «На неделе»", () => {
    // 17:40 + 7 ч 20 мин = 01:00 следующих суток по Москве.
    const rows = groups([deadline("tonight", 7.34), deadline("late", 6)]);
    expect(rows.find((row) => row.group === "today")?.views.map((v) => v.deadline.id)).toEqual(["late"]);
    expect(rows.find((row) => row.group === "week")?.views.map((v) => v.deadline.id)).toEqual(["tonight"]);
  });

  it("срок, назначенный преподавателем, важнее базового", () => {
    const rows = groups([deadline("moved", 24 * 20, "open", 48)]);
    expect(rows.map((row) => row.group)).toEqual(["week"]);
  });

  it("пропущенное остаётся в работе и попадает в «Просрочено», сдано и проверено — в «Сдано»", () => {
    const rows = groups([
      deadline("missed", -48, "missed"),
      deadline("sub", -10, "submitted"),
      deadline("gr", -90, "graded"),
    ]);
    expect(rows.map((row) => row.group)).toEqual(["overdue", "done"]);
    expect(rows[1]?.views.map((v) => v.deadline.id)).toEqual(["sub", "gr"]);
  });

  it("без срока — отдельная группа, без придуманного отсчёта", () => {
    const rows = groups([deadline("none", null)]);
    expect(rows[0]?.title).toBe(GROUP_TITLE.none);
    expect(rows[0]?.views[0]?.urgency).toBe("unknown");
  });
});

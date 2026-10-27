import { effectiveDue, type Urgency, urgencyOf } from "@botai/design";
import type { Deadline } from "@/api/types";
import type { DeadlineState } from "@/features/data/types";

/** Сданное и проверенное — «Сдано». Пропущенное остаётся в работе: срок прошёл, и это видно. */
const DONE_STATES: ReadonlySet<DeadlineState> = new Set(["submitted", "graded"]);

export const DEADLINE_STATE_LABEL: Record<DeadlineState, string> = {
  open: "Нужно сдать",
  submitted: "Сдано",
  graded: "Проверено",
  missed: "Пропущено",
  unknown: "Статус неизвестен",
};

export interface DeadlineView {
  deadline: Deadline;
  /** Срок, по которому считается срочность: назначенный (`effectiveDueAt`) важнее базового. */
  dueMs: number | null;
  urgency: Urgency;
  done: boolean;
}

export function toDeadlineView(deadline: Deadline, nowMs: number): DeadlineView {
  const dueMs = effectiveDue(deadline);
  return { deadline, dueMs, urgency: urgencyOf(dueMs, nowMs), done: DONE_STATES.has(deadline.state) };
}

/** По сроку: ближайшие первыми, без срока — в конце. */
export function sortByDue(views: readonly DeadlineView[]): DeadlineView[] {
  return [...views].sort((a, b) => {
    if (a.dueMs === null && b.dueMs === null) return a.deadline.title.localeCompare(b.deadline.title, "ru");
    if (a.dueMs === null) return 1;
    if (b.dueMs === null) return -1;
    return a.dueMs - b.dueMs || a.deadline.id.localeCompare(b.deadline.id);
  });
}

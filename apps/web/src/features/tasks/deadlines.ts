import { effectiveDue, type Urgency, urgencyOf } from "@botai/design";
import type { Deadline } from "@/api/types";
import { scoreLabel } from "@/features/data/scores";
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

export interface DeadlineStatus {
  /** Статус словами: подпись источника («Решение прикреплено»), а если её нет или статус отметил студент — наш. */
  text: string;
  /** Откуда статус, если не из источника: «отмечено вами», «по вашему правилу». */
  note: string | null;
}

/**
 * Статус задания для строки и карточки (D-043, D-045). Отметка студента сильнее источника: подпись источника тогда
 * говорит о другом состоянии и не показывается, вместо неё — наше слово и «отмечено вами». Правило студента
 * переводит подпись источника в состояние — подпись остаётся, рядом «по вашему правилу».
 */
export function deadlineStatus(deadline: Deadline): DeadlineStatus {
  const label = DEADLINE_STATE_LABEL[deadline.state];
  if (deadline.stateSource === "student") return { text: label, note: "отмечено вами" };
  const text = deadline.stateText?.trim() || label;
  return { text, note: deadline.stateSource === "rule" ? "по вашему правилу" : null };
}

/** Балл проверенной работы, как дал источник: «8 из 10». Нет оценки у задания — `null`. */
export function deadlineScore(deadline: Deadline): string | null {
  const grade = deadline.grade;
  return grade?.value?.trim() ? scoreLabel(grade) : null;
}

/**
 * Статус в строке списка: у сданного — слова источника вместо «Сдано»; у остального — только подпись источника,
 * если она есть (наше «Нужно сдать» рядом со сроком лишнее).
 */
export function deadlineRowStatus(view: DeadlineView): string | null {
  if (view.done) return deadlineStatus(view.deadline).text;
  return view.deadline.stateSource === "student" ? null : view.deadline.stateText?.trim() || null;
}

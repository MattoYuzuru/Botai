import { type DeadlineGroup, deadlineGroup } from "@botai/design";
import { type DeadlineView, sortByDue } from "./deadlines";

/** Порядок групп §7.1: просроченное — сверху, затем «Сегодня / Неделя / Позже / Без срока / Сдано». */
export const GROUP_ORDER: readonly DeadlineGroup[] = ["overdue", "today", "week", "later", "none", "done"];

export const GROUP_TITLE: Record<DeadlineGroup, string> = {
  overdue: "Просрочено",
  today: "Сегодня",
  week: "На неделе",
  later: "Позже",
  none: "Без срока",
  done: "Сдано",
};

/** Пояснение под заголовком: честно говорит, откуда группа. Пустое значение — подписи нет. */
export const GROUP_NOTE: Partial<Record<DeadlineGroup, string>> = {
  none: "Источник не указывает срок: BotAI его не придумывает.",
};

export interface TaskGroup {
  group: DeadlineGroup;
  title: string;
  views: DeadlineView[];
}

/** Задания по группам; пустые группы не показываются. Внутри группы — по сроку, у «Сдано» — недавние первыми. */
export function groupDeadlines(views: readonly DeadlineView[], nowMs: number, timeZone: string): TaskGroup[] {
  const buckets = new Map<DeadlineGroup, DeadlineView[]>();
  for (const view of views) {
    const group = deadlineGroup(view.dueMs, nowMs, timeZone, view.done);
    buckets.set(group, [...(buckets.get(group) ?? []), view]);
  }
  return GROUP_ORDER.flatMap((group) => {
    const rows = buckets.get(group);
    if (!rows || rows.length === 0) return [];
    const sorted = group === "done" ? sortByDue(rows).reverse() : sortByDue(rows);
    return [{ group, title: GROUP_TITLE[group], views: sorted }];
  });
}
